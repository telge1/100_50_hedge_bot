"""Continuous full-history LONG_ADD distance comparison (research-only).

Real continuous semantics: one trade at a time; next entry only after flat close.
No multi-start, no overlapping independent windows.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.backtest_config_loader import resolve_backtest_config
from research.backtests.backtest_report import BacktestResult
from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.continuous_reentry_backtest import run_continuous_reentry_backtests
from research.backtests.long_add_multistart_metrics import (
    analyze_trade,
    normalize_trade_status,
    safe_float,
    variant_dir_name,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / (
    "research/backtests/results/long_add_continuous_full_history_causal_20260720"
)

LONG_ADD_LEVELS = (0.3, 0.5, 0.8, 1.0, 1.2)
TARGET_PROFIT_USDT = 0.015
TP_PROFIT_TARGET_PCT = 0.25
SYMBOL = "APTUSDT"
DIRECTION = "long"
FILL_MODEL = "conservative"
CONFIG_SOURCE = "live"


def _git_status() -> dict[str, Any]:
    status: dict[str, Any] = {"commit": None, "dirty": None, "status_porcelain": ""}
    try:
        status["commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
        porcelain = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=ROOT, text=True
        )
        status["dirty"] = bool(porcelain.strip())
        status["status_porcelain"] = porcelain
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        status["error"] = str(exc)
    return status


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fields})


def _ts(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def analyze_continuous_variant(
    *,
    long_add_pct: float,
    results: list[BacktestResult],
    candles: list[Any],
    candles_loaded: int,
) -> dict[str, Any]:
    from research.backtests.long_add_multistart_metrics import exposure_from_fills

    variant = variant_dir_name(long_add_pct)
    trade_rows: list[dict[str, Any]] = []
    cycle_rows: list[dict[str, Any]] = []

    closed = 0
    open_count = 0
    series_realized = 0.0
    series_mtm = 0.0
    max_cycle = 0
    completed_cycles = 0
    max_long_qty = 0.0
    max_short_qty = 0.0
    max_net_qty = 0.0
    max_total_notional = 0.0
    fees = 0.0
    undercoverage = 0
    pending_final = 0
    same_candle = 0
    exit_rebuilds = 0
    exit_increases = 0
    harmful_rebuilds = 0

    blocker: BacktestResult | None = None
    blocker_analysis: dict[str, Any] | None = None
    realized_until_blocker = 0.0

    for result in results:
        start_index = int(result.start_index or 0)
        window = candles[start_index:]
        analysis = analyze_trade(
            result,
            variant=variant,
            long_add_pct=long_add_pct,
            target_profit_usdt=TARGET_PROFIT_USDT,
            window_candles=window,
            valid=True,
            skip_reason="ok",
        )
        status = normalize_trade_status(result)
        mtm = safe_float(analysis.get("mtm_pnl"))
        realized = safe_float(analysis.get("realized_pnl"))
        series_mtm += mtm
        series_realized += realized
        max_cycle = max(max_cycle, int(analysis.get("max_cycle") or 0))
        completed_cycles += int(analysis.get("completed_cycles") or 0)

        fills = list(result.fill_log or [])
        if fills:
            max_long_qty = max(max_long_qty, max(safe_float(f.get("long_qty_after")) for f in fills))
            max_short_qty = max(
                max_short_qty, max(safe_float(f.get("short_qty_after")) for f in fills)
            )
            max_net_qty = max(
                max_net_qty,
                max(
                    abs(safe_float(f.get("long_qty_after")) - safe_float(f.get("short_qty_after")))
                    for f in fills
                ),
            )
            exp = exposure_from_fills(fills)
            max_total_notional = max(max_total_notional, exp["max_total_notional"])

        fees += safe_float(analysis.get("fees"))
        undercoverage += int(analysis.get("undercoverage") or 0)
        pending_final += int(analysis.get("pending_final_exit") or 0)
        same_candle += int(analysis.get("same_candle_long_add_short_reduce") or 0)
        exit_rebuilds += int(analysis.get("exit_rebuild_count") or 0)
        exit_increases += int(analysis.get("exit_increase_count") or 0)
        harmful_rebuilds += int(analysis.get("old_exit_later_reachable_count") or 0)

        trade_rows.append(
            {
                "variant": variant,
                "long_add_pct": long_add_pct,
                "trade_number": int(result.trade_number or 0),
                "start_index": start_index,
                "end_index": result.end_index,
                "start_timestamp": _ts(result.start_time),
                "end_timestamp": _ts(result.end_time),
                "status": status,
                "exit_reason": result.exit_reason,
                "duration_candles": int(result.candles_processed or 0),
                "realized_pnl": realized,
                "unrealized_pnl": safe_float(analysis.get("unrealized_pnl")),
                "mtm_pnl": mtm,
                "max_cycle": analysis.get("max_cycle"),
                "completed_cycles": analysis.get("completed_cycles"),
                "final_long_qty": analysis.get("final_long_qty"),
                "final_short_qty": analysis.get("final_short_qty"),
                "final_net_qty": analysis.get("final_net_qty"),
                "final_long_avg": analysis.get("final_long_avg"),
                "final_short_avg": analysis.get("final_short_avg"),
                "mark_price_end": analysis.get("mark_price_end"),
                "active_exit_price": analysis.get("active_exit_price"),
                "distance_to_exit": analysis.get("distance_to_exit"),
                "max_total_notional": analysis.get("max_total_notional"),
                "max_abs_net_exposure": analysis.get("max_abs_net_exposure"),
                "fees": analysis.get("fees"),
                "undercoverage": analysis.get("undercoverage"),
                "pending_final_exit": analysis.get("pending_final_exit"),
                "same_candle_long_add_short_reduce": analysis.get(
                    "same_candle_long_add_short_reduce"
                ),
                "exit_rebuild_count": analysis.get("exit_rebuild_count"),
                "exit_increase_count": analysis.get("exit_increase_count"),
                "old_exit_later_reachable_count": analysis.get(
                    "old_exit_later_reachable_count"
                ),
            }
        )
        for cycle in analysis.get("cycle_rows") or []:
            cycle_rows.append({**cycle, "trade_number": int(result.trade_number or 0)})

        if status == "closed":
            closed += 1
            realized_until_blocker += realized
        else:
            open_count += 1
            blocker = result
            blocker_analysis = analysis

    started = len(results)
    closed_rate = (closed / started) if started else 0.0
    open_unrealized = safe_float(blocker_analysis.get("unrealized_pnl")) if blocker_analysis else 0.0
    open_realized = safe_float(blocker_analysis.get("realized_pnl")) if blocker_analysis else 0.0
    open_mtm = safe_float(blocker_analysis.get("mtm_pnl")) if blocker_analysis else 0.0

    summary = {
        "variant": variant,
        "long_add_pct": long_add_pct,
        "target_profit_usdt": TARGET_PROFIT_USDT,
        "tp_profit_target_pct": TP_PROFIT_TARGET_PCT,
        "candles_loaded": candles_loaded,
        "trades_started": started,
        "trades_closed": closed,
        "trades_open": open_count,
        "closed_rate": closed_rate,
        "sum_closed_pnl": sum(
            safe_float(row["realized_pnl"]) for row in trade_rows if row["status"] == "closed"
        ),
        "sum_realized_pnl_all_trades": series_realized,
        "open_unrealized_pnl": open_unrealized,
        "open_realized_pnl": open_realized,
        "open_mtm_pnl": open_mtm,
        "series_mtm": series_mtm,
        "realized_pnl_until_blocker": realized_until_blocker,
        "blocker_trade_number": int(blocker.trade_number) if blocker and blocker.trade_number else None,
        "blocker_start_index": (
            int(blocker.start_index) if blocker and blocker.start_index is not None else None
        ),
        "blocker_start_timestamp": _ts(blocker.start_time) if blocker else "",
        "blocker_duration_candles": int(blocker.candles_processed or 0) if blocker else 0,
        "blocker_end_timestamp": _ts(blocker.end_time) if blocker else "",
        "max_cycle": max_cycle,
        "completed_cycles": completed_cycles,
        "max_long_qty": max_long_qty,
        "max_short_qty": max_short_qty,
        "max_abs_net_exposure": max_net_qty,
        "max_total_notional": max_total_notional,
        "fees": fees,
        "undercoverage": undercoverage,
        "pending_final_exit": pending_final,
        "same_candle_violations": same_candle,
        "final_exit_price": (
            (blocker_analysis or {}).get("active_exit_price") if blocker_analysis else None
        ),
        "mark_price_end": (
            (blocker_analysis or {}).get("mark_price_end")
            if blocker_analysis
            else (trade_rows[-1].get("mark_price_end") if trade_rows else None)
        ),
        "distance_to_exit": (
            (blocker_analysis or {}).get("distance_to_exit") if blocker_analysis else None
        ),
        "exit_rebuild_count": exit_rebuilds,
        "exit_increase_count": exit_increases,
        "old_exit_later_reachable_count": harmful_rebuilds,
        "fill_model": FILL_MODEL,
        "config_source": CONFIG_SOURCE,
        "symbol": SYMBOL,
        "direction": DIRECTION,
    }
    return {
        "summary": summary,
        "trade_rows": trade_rows,
        "cycle_rows": cycle_rows,
        "rebuild_rows": [],
    }


def rank_continuous(summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Priority:
    # 1 undercoverage, 2 same-candle, 3 best series MTM, 4 later/no blocker,
    # 5 more closed, 6 shorter blocker duration, 7 lower max exposure, 8 higher realized.
    ranked = sorted(
        summaries,
        key=lambda row: (
            int(row.get("undercoverage") or 0),
            int(row.get("same_candle_violations") or 0),
            -safe_float(row.get("series_mtm")),
            # later blocker preferred: larger blocker_start_index; no blocker → huge
            -(
                safe_float(row.get("blocker_start_index"), 1e18)
                if int(row.get("trades_open") or 0) > 0
                else 1e18
            ),
            -int(row.get("trades_closed") or 0),
            int(row.get("blocker_duration_candles") or 0),
            safe_float(row.get("max_total_notional")),
            -safe_float(row.get("sum_closed_pnl")),
        ),
    )
    out: list[dict[str, Any]] = []
    for idx, row in enumerate(ranked, start=1):
        payload = dict(row)
        payload["rank"] = idx
        out.append(payload)
    return out


def write_report(
    path: Path,
    *,
    ranked: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    trade_rows: list[dict[str, Any]],
) -> None:
    by_pct = {safe_float(row.get("long_add_pct")): row for row in summaries}
    winner = ranked[0] if ranked else {}
    la05 = by_pct.get(0.5, {})
    la12 = by_pct.get(1.2, {})
    harmful_all = all(int(row.get("old_exit_later_reachable_count") or 0) > 0 for row in summaries)
    harmful_any = any(int(row.get("old_exit_later_reachable_count") or 0) > 0 for row in summaries)

    lines = [
        "# LONG_ADD Continuous Full-History Causal Comparison",
        "",
        f"Generated: `{datetime.now(timezone.utc).isoformat()}`",
        "",
        "## Setup",
        "",
        f"- Symbol `{SYMBOL}` / `{DIRECTION}` / `{FILL_MODEL}` / `{CONFIG_SOURCE}`",
        f"- Fixed: `target_profit_usdt={TARGET_PROFIT_USDT}`, `tp_profit_target_pct={TP_PROFIT_TARGET_PCT}`",
        "- Mode: **real continuous re-entry** (one trade at a time; open trade blocks later entries)",
        f"- Window: full available history from `continuous_start_index=0`",
        f"- Variants: `{', '.join(str(x) for x in LONG_ADD_LEVELS)}`",
        "",
        "## Ranking winner",
        "",
        f"**Best continuous path:** `{winner.get('variant')}` "
        f"(long_add={winner.get('long_add_pct')}%, rank={winner.get('rank')})",
        "",
        "Priority: no undercoverage → no same-candle → best series MTM → later/no blocker → "
        "more closed trades → shorter blocker → lower max exposure → higher realized PnL.",
        "",
        "## Comparison table",
        "",
        "| LONG_ADD | Trades | Closed | Open | Realized | Unrealized | Serien-MTM | Blocker-Trade | Blocker-Start | Blocker-Dauer | Max Cycle |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|",
    ]
    for row in sorted(summaries, key=lambda r: safe_float(r.get("long_add_pct"))):
        lines.append(
            f"| {row.get('long_add_pct')} | {row.get('trades_started')} | {row.get('trades_closed')} | "
            f"{row.get('trades_open')} | {safe_float(row.get('sum_closed_pnl')):.4f} | "
            f"{safe_float(row.get('open_unrealized_pnl')):.4f} | {safe_float(row.get('series_mtm')):.4f} | "
            f"{row.get('blocker_trade_number')} | {row.get('blocker_start_timestamp')} | "
            f"{row.get('blocker_duration_candles')} | {row.get('max_cycle')} |"
        )

    lines.extend(
        [
            "",
            "## Answers",
            "",
            f"1. **Best LONG_ADD on real continuous full history:** `{winner.get('variant')}` "
            f"(series_mtm={safe_float(winner.get('series_mtm')):.4f}, "
            f"closed={winner.get('trades_closed')}, blocker_trade={winner.get('blocker_trade_number')}).",
            "2. **Real trades per variant:**",
        ]
    )
    for row in sorted(summaries, key=lambda r: safe_float(r.get("long_add_pct"))):
        lines.append(
            f"   - `{row.get('variant')}`: started={row.get('trades_started')}, "
            f"closed={row.get('trades_closed')}, open={row.get('trades_open')}"
        )

    # Earliest blocker by start timestamp / start index
    with_blocker = [row for row in summaries if int(row.get("trades_open") or 0) > 0]
    earliest = (
        min(with_blocker, key=lambda row: safe_float(row.get("blocker_start_index"), 1e18))
        if with_blocker
        else None
    )
    lines.append(
        f"3. **First to get stuck (earliest blocker start index):** "
        f"`{(earliest or {}).get('variant')}`"
    )
    lines.append(
        f"4. **Blocker trade / timestamp:** trade "
        f"`{(earliest or {}).get('blocker_trade_number')}` at "
        f"`{(earliest or {}).get('blocker_start_timestamp')}` "
        f"(start_index={(earliest or {}).get('blocker_start_index')})"
    )
    lines.append(
        f"5. **Realized PnL until blocker (that variant):** "
        f"`{safe_float((earliest or {}).get('realized_pnl_until_blocker')):.4f}`"
    )
    lines.append(
        f"6. **Open loss of that blocker (unrealized / MTM):** "
        f"unrealized=`{safe_float((earliest or {}).get('open_unrealized_pnl')):.4f}`, "
        f"mtm=`{safe_float((earliest or {}).get('open_mtm_pnl')):.4f}`"
    )
    lines.append(
        f"7. **Final series MTM (winner / all):** winner "
        f"`{safe_float(winner.get('series_mtm')):.4f}`; "
        + ", ".join(
            f"{row.get('variant')}={safe_float(row.get('series_mtm')):.4f}"
            for row in sorted(summaries, key=lambda r: safe_float(r.get("long_add_pct")))
        )
    )
    better_12 = safe_float(la12.get("series_mtm")) > safe_float(la05.get("series_mtm"))
    lines.append(
        f"8. **Is 1.2% really better than 0.5% in continuous mode?** "
        f"{'Yes' if better_12 else 'No'} on series MTM "
        f"({safe_float(la12.get('series_mtm')):.4f} vs {safe_float(la05.get('series_mtm')):.4f}); "
        f"closed {la12.get('trades_closed')} vs {la05.get('trades_closed')}; "
        f"blocker trade {la12.get('blocker_trade_number')} vs {la05.get('blocker_trade_number')}."
    )
    lines.append(
        f"9. **Exit-rebuild blocker on all variants?** "
        f"any=`{harmful_any}`, all=`{harmful_all}` "
        f"(counts: "
        + ", ".join(
            f"{row.get('variant')}={row.get('old_exit_later_reachable_count')}"
            for row in sorted(summaries, key=lambda r: safe_float(r.get("long_add_pct")))
        )
        + ")."
    )
    lines.append(
        f"10. **Basis for next exit-policy test:** `{winner.get('variant')}` "
        f"— best continuous series MTM under the ranking rules, while keeping "
        f"causality/undercoverage clean. Exit-rebuild policy should be tested on this "
        f"continuous blocker path next."
    )

    lines.extend(
        [
            "",
            "## Ranking",
            "",
            "| rank | variant | series_mtm | closed | open | blocker_trade | blocker_dur | under | same | harmful_rebuilds |",
            "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in ranked:
        lines.append(
            f"| {row.get('rank')} | {row.get('variant')} | {safe_float(row.get('series_mtm')):.4f} | "
            f"{row.get('trades_closed')} | {row.get('trades_open')} | {row.get('blocker_trade_number')} | "
            f"{row.get('blocker_duration_candles')} | {row.get('undercoverage')} | "
            f"{row.get('same_candle_violations')} | {row.get('old_exit_later_reachable_count')} |"
        )

    lines.extend(["", "## All trades", ""])
    lines.append(
        "| variant | trade | status | start | end | duration | realized | unrealized | mtm | max_cycle |"
    )
    lines.append("|---|---:|---|---|---|---:|---:|---:|---:|---:|")
    for row in trade_rows:
        lines.append(
            f"| {row.get('variant')} | {row.get('trade_number')} | {row.get('status')} | "
            f"{row.get('start_timestamp')} | {row.get('end_timestamp')} | {row.get('duration_candles')} | "
            f"{safe_float(row.get('realized_pnl')):.4f} | {safe_float(row.get('unrealized_pnl')):.4f} | "
            f"{safe_float(row.get('mtm_pnl')):.4f} | {row.get('max_cycle')} |"
        )
    lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_matrix(
    *,
    output_root: Path,
    candle_limit: int = 50000,
    long_add_levels: tuple[float, ...] = LONG_ADD_LEVELS,
) -> dict[str, Any]:
    if output_root.exists() and any(output_root.iterdir()):
        raise FileExistsError(
            f"Refusing to overwrite existing output directory: {output_root}"
        )
    output_root.mkdir(parents=True, exist_ok=True)

    live_before = resolve_backtest_config(config_source="live", signal="long", symbol=SYMBOL)
    live_long_add = float(live_before.config.long_fill_distance_pct)
    live_target = float(live_before.config.target_profit_usdt)

    candles = load_candles_for_symbol(SYMBOL, limit=candle_limit)
    summaries: list[dict[str, Any]] = []
    all_trades: list[dict[str, Any]] = []
    all_cycles: list[dict[str, Any]] = []
    rebuild_summaries: list[dict[str, Any]] = []

    for long_add_pct in long_add_levels:
        variant = variant_dir_name(long_add_pct)
        run_dir = output_root / variant
        run_dir.mkdir(parents=True, exist_ok=True)
        print(f"=== continuous {variant} long_add={long_add_pct} ===", flush=True)
        payload = run_continuous_reentry_backtests(
            symbol=SYMBOL,
            direction=DIRECTION,
            candles=candles,
            continuous_start_index=0,
            continuous_window_candles=None,
            config_source=CONFIG_SOURCE,
            fill_model=FILL_MODEL,
            tp_profit_target_pct=TP_PROFIT_TARGET_PCT,
            long_fill_distance_pct=long_add_pct,
            target_profit_usdt=TARGET_PROFIT_USDT,
            output_dir=run_dir,
            write_json=True,
            write_csv=True,
            include_logs=False,
        )
        results = list(payload.get("results") or [])
        # Ensure BacktestResult objects
        if results and isinstance(results[0], dict):
            raise RuntimeError("Expected in-memory BacktestResult objects with fill_log")

        analyzed = analyze_continuous_variant(
            long_add_pct=long_add_pct,
            results=results,
            candles=candles,
            candles_loaded=len(candles),
        )
        summaries.append(analyzed["summary"])
        all_trades.extend(analyzed["trade_rows"])
        all_cycles.extend(analyzed["cycle_rows"])
        rebuild_summaries.append(
            {
                "variant": variant,
                "long_add_pct": long_add_pct,
                "exit_rebuild_count": analyzed["summary"]["exit_rebuild_count"],
                "exit_increase_count": analyzed["summary"]["exit_increase_count"],
                "old_exit_later_reachable_count": analyzed["summary"][
                    "old_exit_later_reachable_count"
                ],
            }
        )
        _write_csv(run_dir / "trades.csv", analyzed["trade_rows"])
        _write_csv(run_dir / "cycles.csv", analyzed["cycle_rows"])
        (run_dir / "variant_summary.json").write_text(
            json.dumps(analyzed["summary"], indent=2, default=str) + "\n",
            encoding="utf-8",
        )

    live_after = resolve_backtest_config(config_source="live", signal="long", symbol=SYMBOL)
    if float(live_after.config.long_fill_distance_pct) != live_long_add:
        raise RuntimeError("Live long_fill_distance_pct changed during run")
    if float(live_after.config.target_profit_usdt) != live_target:
        raise RuntimeError("Live target_profit_usdt changed during run")

    ranked = rank_continuous(summaries)
    blocker_rows = [
        {
            "variant": row.get("variant"),
            "long_add_pct": row.get("long_add_pct"),
            "trades_started": row.get("trades_started"),
            "trades_closed": row.get("trades_closed"),
            "trades_open": row.get("trades_open"),
            "sum_closed_pnl": row.get("sum_closed_pnl"),
            "open_unrealized_pnl": row.get("open_unrealized_pnl"),
            "series_mtm": row.get("series_mtm"),
            "realized_pnl_until_blocker": row.get("realized_pnl_until_blocker"),
            "blocker_trade_number": row.get("blocker_trade_number"),
            "blocker_start_index": row.get("blocker_start_index"),
            "blocker_start_timestamp": row.get("blocker_start_timestamp"),
            "blocker_duration_candles": row.get("blocker_duration_candles"),
            "max_cycle": row.get("max_cycle"),
            "old_exit_later_reachable_count": row.get("old_exit_later_reachable_count"),
        }
        for row in summaries
    ]

    _write_csv(output_root / "continuous_trade_details.csv", all_trades)
    _write_csv(output_root / "continuous_variant_summary.csv", summaries)
    _write_csv(output_root / "continuous_blocker_comparison.csv", blocker_rows)
    _write_csv(output_root / "continuous_cycle_summary.csv", all_cycles)
    _write_csv(output_root / "continuous_exit_rebuild_summary.csv", rebuild_summaries)
    _write_csv(output_root / "ranking.csv", ranked)

    manifest = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "git": _git_status(),
        "mode": "continuous_full_history",
        "continuous_reentry": True,
        "continuous_start_index": 0,
        "data_source": {
            "symbol": SYMBOL,
            "loader": "load_candles_for_symbol",
            "candle_count": len(candles),
            "candle_limit_requested": candle_limit,
        },
        "parameters_common": {
            "target_profit_usdt": TARGET_PROFIT_USDT,
            "tp_profit_target_pct": TP_PROFIT_TARGET_PCT,
            "fill_model": FILL_MODEL,
            "config_source": CONFIG_SOURCE,
            "direction": DIRECTION,
        },
        "variants": [
            {
                "variant": variant_dir_name(pct),
                "long_fill_distance_pct": pct,
            }
            for pct in long_add_levels
        ],
        "live_defaults_unchanged": {
            "long_fill_distance_pct": live_long_add,
            "target_profit_usdt": live_target,
        },
        "output_root": str(output_root),
    }
    (output_root / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    write_report(
        output_root / "REPORT.md",
        ranked=ranked,
        summaries=summaries,
        trade_rows=all_trades,
    )
    return {"ranked": ranked, "summaries": summaries, "output_root": str(output_root)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--candle-limit", type=int, default=50000)
    args = parser.parse_args(argv)
    payload = run_matrix(output_root=args.output_dir, candle_limit=args.candle_limit)
    print(
        json.dumps(
            {
                "output_root": payload["output_root"],
                "ranked": [
                    {
                        "rank": row.get("rank"),
                        "variant": row.get("variant"),
                        "series_mtm": row.get("series_mtm"),
                        "trades_closed": row.get("trades_closed"),
                        "blocker_trade_number": row.get("blocker_trade_number"),
                    }
                    for row in payload["ranked"]
                ],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

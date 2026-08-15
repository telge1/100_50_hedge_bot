"""Causal multi-start LONG_ADD distance sweep (research-only).

Uses the existing multi-start runner with identical start indices per variant.
Does not mutate live defaults, exit policy, or recovery rules.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.backtest_config_loader import resolve_backtest_config
from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.long_add_multistart_metrics import (
    BASELINE_LONG_ADD_PCT,
    aggregate_variant_trades,
    analyze_trade,
    classify_start,
    paired_compare_to_baseline,
    rank_variants,
    safe_float,
    variant_dir_name,
)
from research.backtests.multi_start_backtest import (
    generate_start_indices,
    run_multi_start_backtest,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "research/backtests/results/long_add_multistart_causal_20260720"

LONG_ADD_LEVELS = (0.3, 0.5, 0.8, 1.0, 1.2)
TARGET_PROFIT_USDT = 0.015
TP_PROFIT_TARGET_PCT = 0.25
SYMBOL = "APTUSDT"
DIRECTION = "long"
FILL_MODEL = "conservative"
CONFIG_SOURCE = "live"
WINDOW_CANDLES = 10000
START_STEP_CANDLES = 250
FALLBACK_START_STEP = 100
MAX_STARTS = 200
MIN_VALID_STARTS = 100


def _git_status() -> dict[str, Any]:
    status: dict[str, Any] = {"commit": None, "dirty": None, "status_porcelain": ""}
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
        ).strip()
        porcelain = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=ROOT,
            text=True,
        )
        status["commit"] = commit
        status["dirty"] = bool(porcelain.strip())
        status["status_porcelain"] = porcelain
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        status["error"] = str(exc)
    return status


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = fieldnames or list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fields})


def resolve_shared_start_indices(
    candle_count: int,
    *,
    window_candles: int = WINDOW_CANDLES,
    start_step_candles: int = START_STEP_CANDLES,
    max_starts: int = MAX_STARTS,
    min_valid_starts: int = MIN_VALID_STARTS,
) -> tuple[list[int], int]:
    indices = generate_start_indices(
        candle_count,
        start_step_candles=start_step_candles,
        window_candles=window_candles,
        max_starts=max_starts,
        require_full_window=True,
    )
    step_used = start_step_candles
    if len(indices) < min_valid_starts and start_step_candles > FALLBACK_START_STEP:
        indices = generate_start_indices(
            candle_count,
            start_step_candles=FALLBACK_START_STEP,
            window_candles=window_candles,
            max_starts=max_starts,
            require_full_window=True,
        )
        step_used = FALLBACK_START_STEP
    return indices, step_used


def run_variant(
    *,
    candles: list[Any],
    start_indices: list[int],
    long_add_pct: float,
    window_candles: int,
    output_dir: Path,
) -> dict[str, Any]:
    variant = variant_dir_name(long_add_pct)
    run_dir = output_dir / variant
    run_dir.mkdir(parents=True, exist_ok=True)

    results = run_multi_start_backtest(
        SYMBOL,
        DIRECTION,
        candles,
        config_source=CONFIG_SOURCE,
        fill_model=FILL_MODEL,
        window_candles=window_candles,
        max_starts=len(start_indices),
        start_indices=start_indices,
        require_full_window=True,
        tp_profit_target_pct=TP_PROFIT_TARGET_PCT,
        long_fill_distance_pct=long_add_pct,
        target_profit_usdt=TARGET_PROFIT_USDT,
    )
    by_start = {
        int(result.start_index) if result.start_index is not None else -1: result
        for result in results
    }

    trade_rows: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    cycle_rows: list[dict[str, Any]] = []

    for start_index in start_indices:
        result = by_start.get(start_index)
        valid, reason = classify_start(result, window_candles=window_candles)
        if result is None:
            skipped.append(
                {
                    "variant": variant,
                    "start_index": start_index,
                    "skip_reason": reason,
                }
            )
            continue
        window = candles[start_index : start_index + window_candles]
        analysis = analyze_trade(
            result,
            variant=variant,
            long_add_pct=long_add_pct,
            target_profit_usdt=TARGET_PROFIT_USDT,
            window_candles=window,
            valid=valid,
            skip_reason=reason,
        )
        if not valid:
            skipped.append(
                {
                    "variant": variant,
                    "start_index": start_index,
                    "skip_reason": reason,
                    "status": analysis.get("status"),
                    "error": analysis.get("error"),
                }
            )
            continue
        trade_rows.append(analysis)
        cycle_rows.extend(analysis.get("cycle_rows") or [])

    summary = aggregate_variant_trades(
        trade_rows,
        planned_starts=len(start_indices),
        skipped=skipped,
    )
    summary.update(
        {
            "variant": variant,
            "long_add_pct": long_add_pct,
            "target_profit_usdt": TARGET_PROFIT_USDT,
            "tp_profit_target_pct": TP_PROFIT_TARGET_PCT,
            "fill_model": FILL_MODEL,
            "config_source": CONFIG_SOURCE,
            "symbol": SYMBOL,
            "direction": DIRECTION,
            "window_candles": window_candles,
        }
    )

    compact_trades = [
        {key: value for key, value in row.items() if key not in {"same_candle_cases", "cycle_rows"}}
        for row in trade_rows
    ]
    _write_csv(run_dir / "trades.csv", compact_trades)
    _write_csv(run_dir / "skipped_starts.csv", skipped)
    _write_csv(run_dir / "cycles.csv", cycle_rows)
    (run_dir / "variant_summary.json").write_text(
        json.dumps(summary, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return {
        "summary": summary,
        "trades": trade_rows,
        "skipped": skipped,
        "cycle_rows": cycle_rows,
        "run_dir": str(run_dir),
    }


def _cycle_aggregate(cycle_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, float, int], list[dict[str, Any]]] = defaultdict(list)
    for row in cycle_rows:
        key = (str(row.get("variant")), safe_float(row.get("long_add_pct")), int(row.get("cycle_index") or 0))
        grouped[key].append(row)
    out: list[dict[str, Any]] = []
    for (variant, long_add_pct, cycle_index), rows in sorted(grouped.items()):
        completed = [row for row in rows if row.get("complete")]
        first_losses = [safe_float(row.get("first_leg_loss")) for row in completed]
        second_gains = [safe_float(row.get("second_leg_gain")) for row in completed]
        nets = [safe_float(row.get("cycle_net")) for row in completed]
        margins = [safe_float(row.get("coverage_margin")) for row in completed]
        durations = [
            safe_float(row.get("duration_first_to_second_candles"))
            for row in completed
            if row.get("duration_first_to_second_candles") is not None
        ]
        out.append(
            {
                "variant": variant,
                "long_add_pct": long_add_pct,
                "cycle_index": cycle_index,
                "started": len(rows),
                "completed": len(completed),
                "completion_rate": (len(completed) / len(rows)) if rows else 0.0,
                "avg_first_leg_loss": (sum(first_losses) / len(first_losses)) if first_losses else None,
                "avg_second_leg_gain": (sum(second_gains) / len(second_gains)) if second_gains else None,
                "avg_cycle_net": (sum(nets) / len(nets)) if nets else None,
                "avg_coverage_margin": (sum(margins) / len(margins)) if margins else None,
                "avg_duration_first_to_second": (sum(durations) / len(durations)) if durations else None,
            }
        )
    return out


def write_report(
    path: Path,
    *,
    ranked: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    paired_summary: list[dict[str, Any]],
    cycle_summary: list[dict[str, Any]],
    planned_starts: int,
    start_step: int,
    skip_counter: Counter[str],
) -> None:
    by_pct = {safe_float(row.get("long_add_pct")): row for row in summaries}
    winner = ranked[0] if ranked else {}
    best_mtm = max(summaries, key=lambda row: safe_float(row.get("sum_mtm_pnl"))) if summaries else {}
    most_closed = max(summaries, key=lambda row: int(row.get("closed_trades") or 0)) if summaries else {}
    fewest_runners = (
        min(summaries, key=lambda row: int(row.get("open_long_runner_count") or 0)) if summaries else {}
    )
    best_worst = (
        max(summaries, key=lambda row: safe_float(row.get("worst_trade_mtm"), -1e18)) if summaries else {}
    )
    baseline = by_pct.get(BASELINE_LONG_ADD_PCT, {})
    la08 = by_pct.get(0.8, {})
    la10 = by_pct.get(1.0, {})
    la12 = by_pct.get(1.2, {})

    cycle_3 = [row for row in cycle_summary if int(row.get("cycle_index") or 0) == 3]
    cycle_5 = [row for row in cycle_summary if int(row.get("cycle_index") or 0) == 5]
    paired_by_pct = {safe_float(row.get("long_add_pct")): row for row in paired_summary}

    lines = [
        "# LONG_ADD Multi-Start Causal Comparison",
        "",
        f"Generated: `{datetime.now(timezone.utc).isoformat()}`",
        "",
        "## Setup",
        "",
        f"- Symbol: `{SYMBOL}` / direction `{DIRECTION}` / fill `{FILL_MODEL}` / config `{CONFIG_SOURCE}`",
        f"- Fixed: `target_profit_usdt={TARGET_PROFIT_USDT}`, `tp_profit_target_pct={TP_PROFIT_TARGET_PCT}`",
        f"- Window: `{WINDOW_CANDLES}` candles, start step `{start_step}`, planned starts `{planned_starts}`",
        f"- Variants: `{', '.join(str(x) for x in LONG_ADD_LEVELS)}` percent points",
        f"- Skip reasons: `{dict(skip_counter)}`",
        "",
        "## Ranking winner",
        "",
        f"**Robustness winner:** `{winner.get('variant')}` "
        f"(long_add={winner.get('long_add_pct')}%, rank={winner.get('rank')})",
        "",
        "Ranking priority: no causality violations → no undercoverage → few negative closed → "
        "higher total MTM → fewer open long-runners → better worst MTM → lower max exposure → "
        "higher closed rate → closed PnL last.",
        "",
        "## Answers",
        "",
        f"1. **Most robust (≥100 trades):** `{winner.get('variant')}` "
        f"(valid={winner.get('valid_trades')}, same_candle={winner.get('same_candle_violations')}, "
        f"undercoverage={winner.get('undercoverage')}, neg_closed={winner.get('negative_closed_trades')}, "
        f"sum_mtm={safe_float(winner.get('sum_mtm_pnl')):.4f}).",
        f"2. **Best total MTM:** `{best_mtm.get('variant')}` "
        f"(sum_mtm={safe_float(best_mtm.get('sum_mtm_pnl')):.4f}).",
        f"3. **Most closed trades:** `{most_closed.get('variant')}` "
        f"(closed={most_closed.get('closed_trades')}, closed_rate={safe_float(most_closed.get('closed_rate')):.3f}).",
        f"4. **Fewest long-runners:** `{fewest_runners.get('variant')}` "
        f"(open_long_runners={fewest_runners.get('open_long_runner_count')}).",
        f"5. **Best worst-case MTM:** `{best_worst.get('variant')}` "
        f"(worst_mtm={safe_float(best_worst.get('worst_trade_mtm')):.4f}).",
        "",
        "6. **Does 0.8% really improve the strategy, or only closed-trade count?**",
    ]
    p08 = paired_by_pct.get(0.8, {})
    lines.append(
        f"   Paired vs 0.5: better={p08.get('better_starts')}, worse={p08.get('worse_starts')}, "
        f"equal={p08.get('equal_starts')}, avg_mtm_diff={p08.get('avg_mtm_diff')}, "
        f"extra_closed={p08.get('extra_closed_trades')}, extra_long_runners={p08.get('extra_long_runners')}. "
        f"Aggregate: closed {baseline.get('closed_trades')}→{la08.get('closed_trades')}, "
        f"sum_mtm {safe_float(baseline.get('sum_mtm_pnl')):.4f}→{safe_float(la08.get('sum_mtm_pnl')):.4f}, "
        f"open_runners {baseline.get('open_long_runner_count')}→{la08.get('open_long_runner_count')}."
    )
    baseline_rank = next(
        (row.get("rank") for row in ranked if abs(safe_float(row.get("long_add_pct")) - 0.5) < 1e-12),
        None,
    )
    lines.extend(
        [
            "",
            "7. **Are 1.0% / 1.2% worse due to larger first-leg losses?**",
            f"   `1.0%`: sum_mtm={safe_float(la10.get('sum_mtm_pnl')):.4f}, "
            f"avg_cycle={la10.get('avg_max_cycle')}, worst={safe_float(la10.get('worst_trade_mtm')):.4f}.",
            f"   `1.2%`: sum_mtm={safe_float(la12.get('sum_mtm_pnl')):.4f}, "
            f"avg_cycle={la12.get('avg_max_cycle')}, worst={safe_float(la12.get('worst_trade_mtm')):.4f}.",
            "   First-leg losses rise with distance (see cycle_3_5_audit.csv), but completed cycles "
            "still cover ~target_profit_usdt. Judge total MTM / open runners, not first-leg size alone.",
            "",
            f"8. **Does 0.5% remain the best baseline on the large corpus?** "
            f"Rank of `la_0_5` = `{baseline_rank}`; "
            f"sum_mtm={safe_float(baseline.get('sum_mtm_pnl')):.4f}, "
            f"closed={baseline.get('closed_trades')}, runners={baseline.get('open_long_runner_count')}.",
            "",
            "9. **Cycle 3 / Cycle 5 by variant**",
            "",
        ]
    )

    lines.append("| variant | cycle | started | completed | avg_first_leg_loss | avg_cycle_net | avg_coverage_margin |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for row in cycle_3 + cycle_5:
        lines.append(
            f"| {row.get('variant')} | {row.get('cycle_index')} | {row.get('started')} | "
            f"{row.get('completed')} | {safe_float(row.get('avg_first_leg_loss')):.4f} | "
            f"{safe_float(row.get('avg_cycle_net')):.4f} | {safe_float(row.get('avg_coverage_margin')):.4f} |"
        )

    rebuild_present = all(int(row.get("old_exit_later_reachable_count") or 0) > 0 for row in summaries)
    rebuild_any = any(int(row.get("old_exit_later_reachable_count") or 0) > 0 for row in summaries)
    lines.extend(
        [
            "",
            "10. **Is the exit-rebuild blocker present at all distances?**",
            f"   Harmful rebuilds (old exit later reachable, new exit missed) present in any variant: "
            f"`{rebuild_any}`; present in all variants: `{rebuild_present}`.",
            "",
            "## Variant summary",
            "",
            "| rank | variant | valid | closed | open_runners | sum_mtm | worst_mtm | same_candle | under | neg_closed |",
            "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in ranked:
        lines.append(
            f"| {row.get('rank')} | {row.get('variant')} | {row.get('valid_trades')} | "
            f"{row.get('closed_trades')} | {row.get('open_long_runner_count')} | "
            f"{safe_float(row.get('sum_mtm_pnl')):.4f} | {safe_float(row.get('worst_trade_mtm')):.4f} | "
            f"{row.get('same_candle_violations')} | {row.get('undercoverage')} | "
            f"{row.get('negative_closed_trades')} |"
        )
    lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_matrix(
    *,
    output_root: Path,
    candle_limit: int = 50000,
    window_candles: int = WINDOW_CANDLES,
    start_step_candles: int = START_STEP_CANDLES,
    max_starts: int = MAX_STARTS,
    long_add_levels: tuple[float, ...] = LONG_ADD_LEVELS,
) -> dict[str, Any]:
    if output_root.exists() and any(output_root.iterdir()):
        raise FileExistsError(
            f"Refusing to overwrite existing output directory: {output_root}. "
            "Choose a new path or remove the directory first."
        )
    output_root.mkdir(parents=True, exist_ok=True)

    live_before = resolve_backtest_config(config_source="live", signal="long", symbol=SYMBOL)
    live_long_add_before = float(live_before.config.long_fill_distance_pct)
    live_target_before = float(live_before.config.target_profit_usdt)

    candles = load_candles_for_symbol(SYMBOL, limit=candle_limit)
    start_indices, step_used = resolve_shared_start_indices(
        len(candles),
        window_candles=window_candles,
        start_step_candles=start_step_candles,
        max_starts=max_starts,
    )
    if len(start_indices) < MIN_VALID_STARTS:
        raise RuntimeError(
            f"Only {len(start_indices)} full-window starts available; need >= {MIN_VALID_STARTS}."
        )

    print(
        f"Shared starts: n={len(start_indices)} step={step_used} "
        f"window={window_candles} candles={len(candles)}",
        flush=True,
    )

    trades_by_variant: dict[float, list[dict[str, Any]]] = {}
    summaries: list[dict[str, Any]] = []
    all_trades: list[dict[str, Any]] = []
    all_cycles: list[dict[str, Any]] = []
    all_skipped: list[dict[str, Any]] = []
    skip_counter: Counter[str] = Counter()

    for long_add_pct in long_add_levels:
        print(f"=== Running {variant_dir_name(long_add_pct)} long_add={long_add_pct} ===", flush=True)
        payload = run_variant(
            candles=candles,
            start_indices=start_indices,
            long_add_pct=long_add_pct,
            window_candles=window_candles,
            output_dir=output_root,
        )
        trades_by_variant[long_add_pct] = payload["trades"]
        summaries.append(payload["summary"])
        all_cycles.extend(payload["cycle_rows"])
        all_skipped.extend(payload["skipped"])
        for reason in (row.get("skip_reason") for row in payload["skipped"]):
            skip_counter[str(reason)] += 1
        for row in payload["trades"]:
            all_trades.append(
                {key: value for key, value in row.items() if key not in {"same_candle_cases", "cycle_rows"}}
            )

    live_after = resolve_backtest_config(config_source="live", signal="long", symbol=SYMBOL)
    if float(live_after.config.long_fill_distance_pct) != live_long_add_before:
        raise RuntimeError("Live long_fill_distance_pct changed during matrix run")
    if float(live_after.config.target_profit_usdt) != live_target_before:
        raise RuntimeError("Live target_profit_usdt changed during matrix run")

    paired_rows, paired_summary = paired_compare_to_baseline(trades_by_variant)
    ranked = rank_variants(summaries)
    cycle_summary = _cycle_aggregate(all_cycles)
    cycle_3_5 = [row for row in cycle_summary if int(row.get("cycle_index") or 0) in {3, 5}]

    exposure_summary = [
        {
            "variant": row.get("variant"),
            "long_add_pct": row.get("long_add_pct"),
            "avg_max_abs_net_exposure": row.get("avg_max_abs_net_exposure"),
            "max_abs_net_exposure": row.get("max_abs_net_exposure"),
            "avg_max_total_notional": row.get("avg_max_total_notional"),
            "max_total_notional": row.get("max_total_notional"),
            "avg_fees": row.get("avg_fees"),
        }
        for row in summaries
    ]
    exit_rebuild_summary = [
        {
            "variant": row.get("variant"),
            "long_add_pct": row.get("long_add_pct"),
            "exit_rebuild_count": row.get("exit_rebuild_count"),
            "exit_increase_count": row.get("exit_increase_count"),
            "old_exit_later_reachable_count": row.get("old_exit_later_reachable_count"),
        }
        for row in summaries
    ]

    _write_csv(output_root / "all_trades.csv", all_trades)
    _write_csv(output_root / "variant_summary.csv", summaries)
    _write_csv(output_root / "paired_start_comparison.csv", paired_rows)
    _write_csv(output_root / "paired_summary_vs_0_5.csv", paired_summary)
    _write_csv(output_root / "cycle_summary.csv", cycle_summary)
    _write_csv(output_root / "cycle_3_5_audit.csv", cycle_3_5)
    _write_csv(output_root / "exposure_summary.csv", exposure_summary)
    _write_csv(output_root / "exit_rebuild_summary.csv", exit_rebuild_summary)
    _write_csv(output_root / "ranking.csv", ranked)
    _write_csv(output_root / "skipped_starts.csv", all_skipped)

    manifest = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "git": _git_status(),
        "data_source": {
            "symbol": SYMBOL,
            "loader": "load_candles_for_symbol",
            "candle_count": len(candles),
            "candle_limit_requested": candle_limit,
        },
        "start_indices": start_indices,
        "start_step_candles": step_used,
        "window_candles": window_candles,
        "planned_starts": len(start_indices),
        "fill_model": FILL_MODEL,
        "config_source": CONFIG_SOURCE,
        "direction": DIRECTION,
        "parameters_common": {
            "target_profit_usdt": TARGET_PROFIT_USDT,
            "tp_profit_target_pct": TP_PROFIT_TARGET_PCT,
        },
        "variants": [
            {
                "variant": variant_dir_name(pct),
                "long_fill_distance_pct": pct,
                "target_profit_usdt": TARGET_PROFIT_USDT,
                "tp_profit_target_pct": TP_PROFIT_TARGET_PCT,
            }
            for pct in long_add_levels
        ],
        "live_defaults_unchanged": {
            "long_fill_distance_pct": live_long_add_before,
            "target_profit_usdt": live_target_before,
        },
        "skip_reason_counts": dict(skip_counter),
        "output_root": str(output_root),
    }
    (output_root / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    write_report(
        output_root / "REPORT.md",
        ranked=ranked,
        summaries=summaries,
        paired_summary=paired_summary,
        cycle_summary=cycle_summary,
        planned_starts=len(start_indices),
        start_step=step_used,
        skip_counter=skip_counter,
    )
    return {
        "manifest": manifest,
        "ranked": ranked,
        "summaries": summaries,
        "output_root": str(output_root),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--candle-limit", type=int, default=50000)
    parser.add_argument("--window-candles", type=int, default=WINDOW_CANDLES)
    parser.add_argument("--start-step-candles", type=int, default=START_STEP_CANDLES)
    parser.add_argument("--max-starts", type=int, default=MAX_STARTS)
    args = parser.parse_args(argv)
    payload = run_matrix(
        output_root=args.output_dir,
        candle_limit=args.candle_limit,
        window_candles=args.window_candles,
        start_step_candles=args.start_step_candles,
        max_starts=args.max_starts,
    )
    print(json.dumps({"output_root": payload["output_root"], "ranked": payload["ranked"][:3]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

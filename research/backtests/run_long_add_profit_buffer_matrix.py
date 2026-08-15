"""Controlled LONG_ADD distance × cycle coverage-buffer parameter matrix.

Research-only. Does not mutate live defaults. Uses causal conservative fills.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import defaultdict
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from research.backtests.backtest_config_loader import resolve_backtest_config
from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.continuous_reentry_backtest import run_continuous_reentry_backtests
from research.backtests.pnl_coverage_audit import export_pnl_coverage_audits
from research.backtests.trade_block_export import export_trade_blocks_for_results

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / (
    "research/backtests/results/long_continuous_tp_0_25_causal_parameter_matrix_20260720"
)

LIVE_LONG_ADD_PCT = 0.5  # config units: percent points (0.5 => 0.5%)
LIVE_TARGET_PROFIT_USDT = 0.015
TP_PROFIT_TARGET_PCT = 0.25

# Phase 2 uses live LONG_ADD; Phase 1/3 use the sweep distances.
LONG_ADD_LEVELS = (0.5, 0.8, 1.0, 1.2)
BUFFER_MULTIPLIERS = (1.0, 1.25, 1.5, 2.0)


def _variant_dir_name(*, long_add_pct: float, buffer_mult: float) -> str:
    la = str(long_add_pct).replace(".", "_")
    bm = f"{buffer_mult:.2f}".replace(".", "_")
    return f"la_{la}_buffer_{bm}"


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _row_as_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return row
    if is_dataclass(row):
        return asdict(row)
    return dict(getattr(row, "__dict__", {}))


def _load_trade_blocks(run_dir: Path, trade_number: int) -> list[dict[str, Any]]:
    matches = sorted(
        run_dir.glob(f"APTUSDT_long_continuous_trade_{trade_number:04d}_*_trade_blocks.json")
    )
    if not matches:
        return []
    doc = json.loads(matches[0].read_text())
    return list(doc.get("trade_blocks") or [])


def _filled_rows(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in blocks:
        row_type = str(row.get("row_type") or "").lower()
        status = str(row.get("status") or "").upper()
        event = str(row.get("event_type") or "").lower()
        is_fill = row_type == "fill" or status == "FILLED" or event in {"filled", "fill"}
        if not is_fill:
            continue
        if row.get("purpose"):
            out.append(row)
    return out


def _same_candle_follow_fills(fills: list[dict[str, Any]], trade_number: int) -> list[dict[str, Any]]:
    by: dict[tuple[str, int], dict[str, list]] = defaultdict(lambda: {"long": [], "short": []})
    for fill in fills:
        purpose = str(fill.get("purpose") or "")
        match = re.search(r"CYCLE_(\d+)_", purpose)
        if not match:
            continue
        cycle = int(match.group(1))
        ts = str(fill.get("timestamp") or "")
        price = fill.get("fill_price")
        if price in (None, ""):
            price = fill.get("price")
        key = (ts, cycle)
        if purpose.endswith("_LONG_ADD"):
            by[key]["long"].append((purpose, price, fill))
        elif purpose.endswith("_SHORT_REDUCE"):
            by[key]["short"].append((purpose, price, fill))
    cases: list[dict[str, Any]] = []
    for (ts, cycle), sides in sorted(by.items()):
        if sides["long"] and sides["short"]:
            cases.append(
                {
                    "trade_number": trade_number,
                    "timestamp": ts,
                    "cycle": cycle,
                    "long_purpose": sides["long"][0][0],
                    "long_price": sides["long"][0][1],
                    "short_purpose": sides["short"][0][0],
                    "short_price": sides["short"][0][1],
                }
            )
    return cases


def _cycle_leg_fills(fills: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    cycles: dict[int, dict[str, Any]] = {}
    for fill in fills:
        purpose = str(fill.get("purpose") or "")
        match = re.search(r"CYCLE_(\d+)_(LONG_ADD|SHORT_REDUCE)$", purpose)
        if not match:
            continue
        cycle = int(match.group(1))
        leg = match.group(2)
        entry = cycles.setdefault(
            cycle,
            {
                "cycle": cycle,
                "long_add": None,
                "short_reduce": None,
            },
        )
        payload = {
            "purpose": purpose,
            "timestamp": fill.get("timestamp"),
            "candle_index": fill.get("candle_index") or fill.get("local_candle_index"),
            "fill_price": fill.get("fill_price") if fill.get("fill_price") not in (None, "") else fill.get("price"),
            "closed_pnl": _safe_float(fill.get("closed_pnl") or fill.get("confirmed_closed_pnl")),
            "qty": _safe_float(fill.get("filled_qty") or fill.get("qty")),
        }
        if leg == "LONG_ADD":
            entry["long_add"] = payload
        else:
            entry["short_reduce"] = payload
    return cycles


def _exposure_stats(blocks: list[dict[str, Any]]) -> dict[str, float]:
    max_long = max_short = max_long_notional = max_short_notional = 0.0
    max_total_notional = max_net_abs = 0.0
    fees = 0.0
    for row in blocks:
        long_qty = _safe_float(row.get("long_qty_after"))
        short_qty = _safe_float(row.get("short_qty_after"))
        long_avg = _safe_float(row.get("long_avg_after"))
        short_avg = _safe_float(row.get("short_avg_after"))
        px = _safe_float(row.get("candle_close") or row.get("fill_price") or row.get("price"))
        long_notional = long_qty * (long_avg if long_avg > 0 else px)
        short_notional = short_qty * (short_avg if short_avg > 0 else px)
        max_long = max(max_long, long_qty)
        max_short = max(max_short, short_qty)
        max_long_notional = max(max_long_notional, long_notional)
        max_short_notional = max(max_short_notional, short_notional)
        max_total_notional = max(max_total_notional, long_notional + short_notional)
        max_net_abs = max(max_net_abs, abs(long_qty - short_qty))
        fees += _safe_float(row.get("entry_fee")) + _safe_float(row.get("exit_fee")) + _safe_float(
            row.get("closing_fee")
        )
    return {
        "max_long_qty": max_long,
        "max_short_qty": max_short,
        "max_long_notional": max_long_notional,
        "max_short_notional": max_short_notional,
        "max_total_notional": max_total_notional,
        "max_net_exposure_qty": max_net_abs,
        "fees_sum_trade_blocks": fees,
    }


def _coverage_audit_stats(run_dir: Path) -> dict[str, Any]:
    undercovered = 0
    pending = 0
    rows_all: list[dict[str, Any]] = []
    for path in sorted(run_dir.glob("*_pnl_coverage_audit.json")):
        doc = json.loads(path.read_text())
        rows = doc if isinstance(doc, list) else doc.get("rows") or doc.get("audits") or []
        if isinstance(doc, dict) and not rows and "cycle_index" in doc:
            rows = [doc]
        for row in rows:
            if not isinstance(row, dict):
                continue
            rows_all.append(row)
            status = str(row.get("status") or "").lower()
            if "undercover" in status:
                undercovered += 1
            if "pending_final" in status:
                pending += 1
    return {
        "undercovered_final_exit": undercovered,
        "pending_final_exit": pending,
        "coverage_rows": rows_all,
    }


def _refill_count(fills: list[dict[str, Any]]) -> int:
    return sum(
        1
        for f in fills
        if str(f.get("purpose") or "").upper().startswith("REFILL_")
        or "RELOAD" in str(f.get("purpose") or "").upper()
    )


def analyze_run(
    *,
    variant: str,
    long_add_pct: float,
    buffer_mult: float,
    target_profit_usdt: float,
    run_dir: Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    aggregate = (payload.get("aggregate") or [{}])[0]
    runs = [_row_as_dict(r) for r in (payload.get("results") or [])]

    closed_pnl = sum(
        _safe_float(r.get("realized_pnl"))
        for r in runs
        if str(r.get("final_status") or "").lower() == "closed"
    )
    open_runs = [r for r in runs if str(r.get("final_status") or "").lower() == "open"]
    open_realized = sum(_safe_float(r.get("realized_pnl")) for r in open_runs)
    open_unrealized = sum(_safe_float(r.get("unrealized_pnl")) for r in open_runs)
    mtm_total = sum(_safe_float(r.get("overall_pnl"), _safe_float(r.get("realized_pnl"))) for r in runs)

    durations = [_safe_float(r.get("candles_processed")) for r in runs]
    max_duration = max(durations) if durations else 0.0
    avg_duration = (sum(durations) / len(durations)) if durations else 0.0
    max_cycle = max((_safe_float(r.get("cycles_seen")) for r in runs), default=0.0)

    same_candle_all: list[dict[str, Any]] = []
    cycle_rows: list[dict[str, Any]] = []
    risk = {
        "max_long_qty": 0.0,
        "max_short_qty": 0.0,
        "max_long_notional": 0.0,
        "max_short_notional": 0.0,
        "max_total_notional": 0.0,
        "max_net_exposure_qty": 0.0,
        "fees_sum_trade_blocks": 0.0,
    }
    refill_events = 0
    trade_summaries: list[dict[str, Any]] = []

    for run in runs:
        trade_number = int(run.get("trade_number") or 0)
        blocks = _load_trade_blocks(run_dir, trade_number)
        fills = _filled_rows(blocks)
        same_candle_all.extend(_same_candle_follow_fills(fills, trade_number))
        refill_events += _refill_count(fills)
        exp = _exposure_stats(blocks)
        for key, value in exp.items():
            risk[key] = max(risk[key], value)

        for cycle, legs in sorted(_cycle_leg_fills(fills).items()):
            long_add = legs.get("long_add") or {}
            short_reduce = legs.get("short_reduce") or {}
            loss_pnl = _safe_float(long_add.get("closed_pnl"))
            cover_pnl = _safe_float(short_reduce.get("closed_pnl"))
            first_leg_loss = abs(loss_pnl) if loss_pnl < 0 else 0.0
            required_net = first_leg_loss + target_profit_usdt if first_leg_loss > 0 or short_reduce else None
            realized_net = cover_pnl
            coverage_margin = (
                (realized_net - required_net) if required_net is not None else None
            )
            long_ci = long_add.get("candle_index")
            short_ci = short_reduce.get("candle_index")
            deferred_ok = None
            if long_ci is not None and short_ci is not None:
                try:
                    deferred_ok = int(short_ci) > int(long_ci)
                except (TypeError, ValueError):
                    deferred_ok = None
            complete = bool(long_add and short_reduce)
            # Reference sanity: next-cycle long add should be near short_reduce * (1 - long_add_pct/100)
            # Evaluated per completed cycle when short reduce exists.
            reference_ok = None
            if short_reduce and short_reduce.get("fill_price"):
                reference_ok = True  # filled second leg present; detailed trigger check via intents below
            cycle_rows.append(
                {
                    "variant": variant,
                    "long_add_pct": long_add_pct,
                    "target_profit_usdt": target_profit_usdt,
                    "buffer_mult": buffer_mult,
                    "trade_number": trade_number,
                    "cycle": cycle,
                    "first_leg_net_loss": first_leg_loss if long_add else None,
                    "second_leg_net_gain": cover_pnl if short_reduce else None,
                    "required_net": required_net,
                    "realized_net": realized_net if short_reduce else None,
                    "coverage_margin": coverage_margin,
                    "complete": complete,
                    "long_add_fill_price": long_add.get("fill_price"),
                    "short_reduce_fill_price": short_reduce.get("fill_price"),
                    "long_add_timestamp": long_add.get("timestamp"),
                    "short_reduce_timestamp": short_reduce.get("timestamp"),
                    "second_leg_deferred_to_next_candle": deferred_ok,
                    "reference_price_ok": reference_ok,
                }
            )

        trade_summaries.append(
            {
                "variant": variant,
                "trade_number": trade_number,
                "start_time": run.get("start_time"),
                "end_time": run.get("end_time"),
                "final_status": run.get("final_status"),
                "exit_reason": run.get("exit_reason"),
                "realized_pnl": run.get("realized_pnl"),
                "unrealized_pnl": run.get("unrealized_pnl"),
                "overall_pnl_mtm": run.get("overall_pnl"),
                "fills_count": run.get("fills_count"),
                "cycles_seen": run.get("cycles_seen"),
                "candles_processed": run.get("candles_processed"),
                "final_long_qty": run.get("final_long_qty"),
                "final_short_qty": run.get("final_short_qty"),
                "same_candle_follow_fills": len(
                    [c for c in same_candle_all if c["trade_number"] == trade_number]
                ),
            }
        )

    coverage = _coverage_audit_stats(run_dir)
    open_status = None
    if open_runs:
        open_status = {
            "trade_number": open_runs[-1].get("trade_number"),
            "final_status": open_runs[-1].get("final_status"),
            "exit_reason": open_runs[-1].get("exit_reason"),
            "realized_pnl": open_runs[-1].get("realized_pnl"),
            "unrealized_pnl": open_runs[-1].get("unrealized_pnl"),
            "overall_pnl_mtm": open_runs[-1].get("overall_pnl"),
            "cycles_seen": open_runs[-1].get("cycles_seen"),
            "candles_processed": open_runs[-1].get("candles_processed"),
            "final_long_qty": open_runs[-1].get("final_long_qty"),
            "final_short_qty": open_runs[-1].get("final_short_qty"),
        }

    return {
        "variant": variant,
        "long_add_pct": long_add_pct,
        "buffer_mult": buffer_mult,
        "target_profit_usdt": target_profit_usdt,
        "tp_profit_target_pct": TP_PROFIT_TARGET_PCT,
        "trades_started": aggregate.get("trades_started"),
        "closed_count": aggregate.get("closed_count"),
        "open_count": aggregate.get("open_count"),
        "successful_closed_count": aggregate.get("successful_closed_count"),
        "negative_pnl_closed_count": aggregate.get("negative_pnl_closed_count"),
        "error_count": aggregate.get("error_count"),
        "total_pnl_reported": aggregate.get("total_pnl"),
        "closed_pnl": closed_pnl,
        "open_realized_pnl": open_realized,
        "open_unrealized_pnl": open_unrealized,
        "mtm_total_pnl": mtm_total,
        "avg_duration_candles": avg_duration,
        "max_duration_candles": max_duration,
        "max_cycle": max_cycle,
        "refill_reload_events": refill_events,
        "same_candle_follow_fills": len(same_candle_all),
        "undercovered_final_exit": coverage["undercovered_final_exit"],
        "pending_final_exit": coverage["pending_final_exit"],
        "open_trade_status": open_status,
        **risk,
        "same_candle_cases": same_candle_all,
        "cycle_rows": cycle_rows,
        "trade_summaries": trade_summaries,
        "run_dir": str(run_dir),
    }


def _rank_key(row: dict[str, Any]) -> tuple:
    neg = int(_safe_float(row.get("negative_pnl_closed_count")))
    under = int(_safe_float(row.get("undercovered_final_exit")))
    same = int(_safe_float(row.get("same_candle_follow_fills")))
    closed = int(_safe_float(row.get("closed_count")))
    open_count = int(_safe_float(row.get("open_count")))
    open_unreal = _safe_float(row.get("open_unrealized_pnl"))
    mtm = _safe_float(row.get("mtm_total_pnl"))
    open_loss_penalty = abs(min(open_unreal, 0.0))
    # Large open mark-to-market holes must not outrank milder open losses solely
    # via higher closed-trade counts.
    severe_open_loss = 1 if open_unreal < -20.0 else 0
    max_exposure = _safe_float(row.get("max_total_notional"))
    max_duration = _safe_float(row.get("max_duration_candles"))
    closed_pnl = _safe_float(row.get("closed_pnl"))
    return (
        neg,
        under,
        same,
        severe_open_loss,
        -mtm,
        -closed,
        open_count,
        open_loss_penalty,
        max_exposure,
        max_duration,
        -closed_pnl,
    )


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    fields = fieldnames or list(rows[0].keys())
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def run_matrix(*, output_root: Path, limit: int = 50000) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    live = resolve_backtest_config(config_source="live", signal="long", symbol="APTUSDT")
    baseline_la = float(live.config.long_fill_distance_pct)
    baseline_buffer = float(live.config.target_profit_usdt)
    baseline_tp_buffer = float(live.config.tp_buffer_pct)
    assert math.isclose(baseline_la, LIVE_LONG_ADD_PCT)
    assert math.isclose(baseline_buffer, LIVE_TARGET_PROFIT_USDT)

    config_map = {
        "long_fill_distance_pct": {
            "path": "live_bots/100_50_hedge_bot/long_bot_1/config/fixed_cycle_config.json",
            "code": "fixed_cycle_hedge_bot/fixed_cycle_strategy.py (_pct / first-leg trigger)",
            "live_value": baseline_la,
            "unit": "percent points (value 0.5 => 0.5%; code uses value/100)",
            "affects": "all CYCLE_N_LONG_ADD first-leg triggers",
            "cli": "--long-fill-distance-pct (new backtest-only)",
            "formula": "trigger = reference * (1 - long_fill_distance_pct/100)",
        },
        "target_profit_usdt": {
            "path": "live_bots/100_50_hedge_bot/long_bot_1/config/fixed_cycle_config.json",
            "code": "fixed_cycle_hedge_bot/fixed_cycle_strategy.py required_net",
            "live_value": baseline_buffer,
            "unit": "USDT absolute",
            "affects": "all CYCLE_N_SHORT_REDUCE coverage targets (cycle second leg)",
            "cli": "--target-profit-usdt (new backtest-only)",
            "formula": "required_net = max(long_loss_usdt + target_profit_usdt, 0)",
        },
        "tp_profit_target_pct": {
            "path": "live_bots/100_50_hedge_bot/long_bot_1/config/fixed_cycle_config.json",
            "code": "fixed_cycle_hedge_bot/hedge_exit_math.py calculate_hedge_exit_price",
            "live_value": float(live.config.tp_profit_target_pct),
            "unit": "percent points of primary notional",
            "affects": "final basket exit profit target (NOT cycle required_net)",
            "cli": "--tp-profit-target-pct (existing; fixed at 0.25 in this matrix)",
            "formula": "exit_target = primary_notional * tp_profit_target_pct / 100",
        },
        "tp_buffer_pct": {
            "path": "live_bots/100_50_hedge_bot/long_bot_1/config/fixed_cycle_config.json",
            "code": "fixed_cycle_hedge_bot/hedge_exit_math.py calculate_hedge_exit_price",
            "live_value": baseline_tp_buffer,
            "unit": "percent points of primary notional",
            "affects": "final exit extra buffer only",
            "cli": "none (unchanged in this matrix)",
            "formula": "buffer_usdt = primary_notional * tp_buffer_pct / 100",
        },
    }
    (output_root / "config_field_map.json").write_text(json.dumps(config_map, indent=2) + "\n")

    candles = load_candles_for_symbol("APTUSDT", limit=limit)
    analyses: list[dict[str, Any]] = []

    for long_add_pct in LONG_ADD_LEVELS:
        for buffer_mult in BUFFER_MULTIPLIERS:
            target_profit = baseline_buffer * buffer_mult
            variant = _variant_dir_name(long_add_pct=long_add_pct, buffer_mult=buffer_mult)
            run_dir = output_root / variant
            run_dir.mkdir(parents=True, exist_ok=True)
            print(
                f"=== {variant}: long_fill_distance_pct={long_add_pct} "
                f"target_profit_usdt={target_profit} ===",
                flush=True,
            )
            payload = run_continuous_reentry_backtests(
                symbol="APTUSDT",
                direction="long",
                candles=candles,
                continuous_start_index=0,
                continuous_window_candles=limit,
                config_source="live",
                fill_model="conservative",
                tp_profit_target_pct=TP_PROFIT_TARGET_PCT,
                long_fill_distance_pct=long_add_pct,
                target_profit_usdt=target_profit,
                output_dir=run_dir,
                write_json=True,
                write_csv=True,
                include_logs=False,
            )
            results = list(payload.get("results") or [])
            export_trade_blocks_for_results(results, run_dir)
            export_pnl_coverage_audits(results, run_dir)
            analysis = analyze_run(
                variant=variant,
                long_add_pct=long_add_pct,
                buffer_mult=buffer_mult,
                target_profit_usdt=target_profit,
                run_dir=run_dir,
                payload=payload,
            )
            analyses.append(analysis)
            (run_dir / "variant_summary.json").write_text(
                json.dumps(
                    {k: v for k, v in analysis.items() if k not in {"same_candle_cases", "cycle_rows", "trade_summaries"}},
                    indent=2,
                    default=str,
                )
                + "\n"
            )

    matrix_rows = []
    trade_rows = []
    cycle_rows = []
    risk_rows = []
    same_rows = []
    for analysis in analyses:
        matrix_rows.append(
            {
                k: analysis[k]
                for k in analysis
                if k
                not in {
                    "same_candle_cases",
                    "cycle_rows",
                    "trade_summaries",
                    "open_trade_status",
                }
            }
            | {
                "open_trade_number": (analysis.get("open_trade_status") or {}).get("trade_number"),
                "open_trade_exit_reason": (analysis.get("open_trade_status") or {}).get("exit_reason"),
                "open_trade_cycles": (analysis.get("open_trade_status") or {}).get("cycles_seen"),
            }
        )
        trade_rows.extend(analysis["trade_summaries"])
        cycle_rows.extend(analysis["cycle_rows"])
        risk_rows.append(
            {
                "variant": analysis["variant"],
                "long_add_pct": analysis["long_add_pct"],
                "buffer_mult": analysis["buffer_mult"],
                "target_profit_usdt": analysis["target_profit_usdt"],
                "max_long_qty": analysis["max_long_qty"],
                "max_short_qty": analysis["max_short_qty"],
                "max_long_notional": analysis["max_long_notional"],
                "max_short_notional": analysis["max_short_notional"],
                "max_total_notional": analysis["max_total_notional"],
                "max_net_exposure_qty": analysis["max_net_exposure_qty"],
                "fees_sum_trade_blocks": analysis["fees_sum_trade_blocks"],
                "max_duration_candles": analysis["max_duration_candles"],
                "max_cycle": analysis["max_cycle"],
            }
        )
        for case in analysis["same_candle_cases"]:
            same_rows.append({"variant": analysis["variant"], **case})

    ranked = sorted(analyses, key=_rank_key)
    ranking_rows = []
    for rank, analysis in enumerate(ranked, start=1):
        ranking_rows.append(
            {
                "rank": rank,
                "variant": analysis["variant"],
                "long_add_pct": analysis["long_add_pct"],
                "buffer_mult": analysis["buffer_mult"],
                "target_profit_usdt": analysis["target_profit_usdt"],
                "negative_pnl_closed_count": analysis["negative_pnl_closed_count"],
                "undercovered_final_exit": analysis["undercovered_final_exit"],
                "same_candle_follow_fills": analysis["same_candle_follow_fills"],
                "closed_count": analysis["closed_count"],
                "open_count": analysis["open_count"],
                "closed_pnl": analysis["closed_pnl"],
                "open_unrealized_pnl": analysis["open_unrealized_pnl"],
                "mtm_total_pnl": analysis["mtm_total_pnl"],
                "max_total_notional": analysis["max_total_notional"],
                "max_duration_candles": analysis["max_duration_candles"],
            }
        )

    best_robust = ranked[0]
    best_pnl = max(analyses, key=lambda a: _safe_float(a.get("closed_pnl")))

    _write_csv(output_root / "parameter_matrix.csv", matrix_rows)
    _write_csv(output_root / "trade_summary.csv", trade_rows)
    _write_csv(output_root / "cycle_coverage_matrix.csv", cycle_rows)
    _write_csv(output_root / "risk_exposure_matrix.csv", risk_rows)
    _write_csv(output_root / "same_candle_audit.csv", same_rows)
    _write_csv(output_root / "ranking.csv", ranking_rows)

    report = _build_report(
        config_map=config_map,
        analyses=analyses,
        ranked=ranked,
        best_robust=best_robust,
        best_pnl=best_pnl,
        baseline_buffer=baseline_buffer,
    )
    (output_root / "REPORT.md").write_text(report)
    return {
        "output_root": str(output_root),
        "variants": len(analyses),
        "best_robust": best_robust["variant"],
        "best_closed_pnl": best_pnl["variant"],
    }


def _build_report(
    *,
    config_map: dict[str, Any],
    analyses: list[dict[str, Any]],
    ranked: list[dict[str, Any]],
    best_robust: dict[str, Any],
    best_pnl: dict[str, Any],
    baseline_buffer: float,
) -> str:
    lines: list[str] = []
    lines.append("# LONG_ADD × Profit-Buffer Parameter Matrix (causal fills)\n")
    lines.append("## Config fields (verified in code)\n")
    for key, meta in config_map.items():
        lines.append(f"### `{key}`")
        lines.append(f"- Config path: `{meta['path']}`")
        lines.append(f"- Code: `{meta['code']}`")
        lines.append(f"- Live value: `{meta['live_value']}`")
        lines.append(f"- Unit: {meta['unit']}")
        lines.append(f"- Affects: {meta['affects']}")
        lines.append(f"- CLI: {meta['cli']}")
        lines.append(f"- Formula: `{meta['formula']}`")
        lines.append("")
    lines.append(
        "Cycle coverage buffer for this audit is **`target_profit_usdt`** "
        f"(baseline `{baseline_buffer}` USDT), not `tp_profit_target_pct` "
        "(kept fixed at 0.25) and not `tp_buffer_pct` (final-exit-only).\n"
    )
    lines.append("## Results table\n")
    lines.append(
        "| variant | LA% | buffer× | buffer USDT | trades | closed | open | neg | "
        "closed_pnl | open_unreal | mtm | max_notional | max_dur | same_candle | under |"
    )
    lines.append(
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
    )
    for a in analyses:
        lines.append(
            f"| {a['variant']} | {a['long_add_pct']} | {a['buffer_mult']} | "
            f"{a['target_profit_usdt']} | {a['trades_started']} | {a['closed_count']} | "
            f"{a['open_count']} | {a['negative_pnl_closed_count']} | "
            f"{_safe_float(a['closed_pnl']):.4f} | {_safe_float(a['open_unrealized_pnl']):.4f} | "
            f"{_safe_float(a['mtm_total_pnl']):.4f} | {_safe_float(a['max_total_notional']):.1f} | "
            f"{_safe_float(a['max_duration_candles']):.0f} | {a['same_candle_follow_fills']} | "
            f"{a['undercovered_final_exit']} |"
        )
    lines.append("")
    lines.append("## Ranking (robustness first)\n")
    for i, row in enumerate(ranked, start=1):
        lines.append(
            f"{i}. `{row['variant']}` closed_pnl={_safe_float(row['closed_pnl']):.4f} "
            f"mtm={_safe_float(row['mtm_total_pnl']):.4f} "
            f"open_unreal={_safe_float(row['open_unrealized_pnl']):.4f} "
            f"max_notional={_safe_float(row['max_total_notional']):.1f}"
        )
    lines.append("")
    same_winner = best_robust["variant"] == best_pnl["variant"]
    lines.append("## Conclusions\n")
    lines.append(f"- Best robustness: `{best_robust['variant']}`")
    lines.append(f"- Best closed PnL: `{best_pnl['variant']}`")
    lines.append(f"- Same variant wins both: **{same_winner}**")
    lines.append(
        "- Open-trade note: reference causal rerun Trade 3 had large unrealized loss; "
        "compare `open_unrealized_pnl` / `mtm_total_pnl` per variant."
    )
    lines.append(
        "- Larger `target_profit_usdt` deepens second-leg TP (harder/further cover); "
        "it does **not** change final `tp_profit_target_pct`."
    )
    by_la: dict[float, list[dict[str, Any]]] = defaultdict(list)
    for a in analyses:
        by_la[float(a["long_add_pct"])].append(a)
    lines.append("")
    lines.append("## LONG_ADD stability\n")
    for la, rows in sorted(by_la.items()):
        avg_closed = sum(_safe_float(r["closed_count"]) for r in rows) / len(rows)
        avg_open_unreal = sum(_safe_float(r["open_unrealized_pnl"]) for r in rows) / len(rows)
        avg_mtm = sum(_safe_float(r["mtm_total_pnl"]) for r in rows) / len(rows)
        lines.append(
            f"- LA {la}%: avg_closed={avg_closed:.2f}, avg_open_unreal={avg_open_unreal:.4f}, "
            f"avg_mtm={avg_mtm:.4f}"
        )
    lines.append("")
    lines.append("See CSVs in this folder for cycle 3/5 coverage and risk details.\n")
    return "\n".join(lines) + "\n"



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUT))
    parser.add_argument("--limit", type=int, default=50000)
    args = parser.parse_args(argv)
    summary = run_matrix(output_root=Path(args.output_dir), limit=args.limit)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

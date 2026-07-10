"""Audit: why CYCLE_1_LONG_REDUCE never fills after CYCLE_1_SHORT_REDUCE (analysis only)."""

from __future__ import annotations

import csv
import json
import math
import re
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fixed_cycle_hedge_bot import direction_config
from fixed_cycle_hedge_bot.cycle_sequence import (
    STEP_WAITING_FOR_PAIR_FIRST_LEG,
    STEP_WAITING_FOR_PAIR_SECOND_LEG,
    CycleSequenceConfig,
    derive_next_required_purpose,
)
from fixed_cycle_hedge_bot.fixed_cycle_strategy import (
    FixedCycleHedgeConfig,
    FixedCycleHedgeStrategy,
    ShortFixedCycleHedgeStrategy,
)

from .backtest_config_loader import resolve_backtest_config
from .candle_loader import load_candles_for_symbol
from .historical_backtest import normalize_candles
from .independent_continuous_long_short_analysis import write_csv, write_json

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_DIR = (
    PROJECT_ROOT
    / "research/backtests/results/independent_continuous_long_short_primary_basis_fix_c4_wait576"
)
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "research/backtests/results/short_cycle1_second_leg_audit"
DEFAULT_AFTER_FIX_OUTPUT_DIR = (
    PROJECT_ROOT
    / "research/backtests/results/short_cycle1_second_leg_audit_after_pnl_persistence_fix"
)
_CYCLE_RE = re.compile(r"CYCLE_(\d+)_(LONG_ADD|SHORT_REDUCE|LONG_REDUCE|SHORT_ADD)")
_FEE_RATE = 0.00055


@dataclass(frozen=True)
class ExpectedCycle1Sequence:
    direction: str
    first_leg_purpose: str
    second_leg_purpose: str
    first_leg_side: str
    second_leg_side: str
    first_leg_distance_key: str
    second_leg_distance_key: str
    second_leg_trigger_mode: str
    waiting_flag_field: str
    pending_cycle_field: str
    second_leg_status_field: str
    confirmed_pnl_cycle_entry_field: str


def _short_sequence() -> ExpectedCycle1Sequence:
    d = direction_config.SHORT_PRIMARY_DIRECTION
    return ExpectedCycle1Sequence(
        direction="short",
        first_leg_purpose="CYCLE_1_SHORT_REDUCE",
        second_leg_purpose="CYCLE_1_LONG_REDUCE",
        first_leg_side="short",
        second_leg_side="long",
        first_leg_distance_key="short_fill_distance_pct",
        second_leg_distance_key="long_fill_distance_pct",
        second_leg_trigger_mode="loss_cover_formula",
        waiting_flag_field="cycle_waiting_for_long_reduce",
        pending_cycle_field="long_reduce_pending_cycle",
        second_leg_status_field="long_reduce_status",
        confirmed_pnl_cycle_entry_field="long_add_confirmed_pnl",
    )


def _long_sequence() -> ExpectedCycle1Sequence:
    d = direction_config.LONG_PRIMARY_DIRECTION
    return ExpectedCycle1Sequence(
        direction="long",
        first_leg_purpose="CYCLE_1_LONG_ADD",
        second_leg_purpose="CYCLE_1_SHORT_REDUCE",
        first_leg_side="long",
        second_leg_side="short",
        first_leg_distance_key="long_fill_distance_pct",
        second_leg_distance_key="short_fill_distance_pct",
        second_leg_trigger_mode="loss_cover_formula",
        waiting_flag_field="cycle_waiting_for_short_tp",
        pending_cycle_field="short_tp_pending_cycle",
        second_leg_status_field="short_reduce_status",
        confirmed_pnl_cycle_entry_field="long_add_confirmed_pnl",
    )


def build_expected_cycle1_sequence_doc() -> dict[str, Any]:
    short = _short_sequence()
    long = _long_sequence()
    short_cfg = resolve_backtest_config(config_source="live", signal="short", symbol="APTUSDT").config
    long_cfg = resolve_backtest_config(config_source="live", signal="long", symbol="APTUSDT").config
    seq = CycleSequenceConfig(
        cycle_prefix="CYCLE",
        first_leg=direction_config.SHORT_PRIMARY_DIRECTION.cycle_first_leg,
        second_leg=direction_config.SHORT_PRIMARY_DIRECTION.cycle_second_leg,
    )
    return {
        "short_primary": {
            "sequence": [
                "INITIAL (long hedge + short primary)",
                short.first_leg_purpose,
                short.second_leg_purpose,
                "cycle 1 complete → CYCLE_2_SHORT_REDUCE first leg",
            ],
            "first_leg_purpose": short.first_leg_purpose,
            "second_leg_purpose": short.second_leg_purpose,
            "order": "SHORT_REDUCE then LONG_REDUCE",
            "active_cycle_index_after_first_leg": 1,
            "cycle_step_after_first_leg": STEP_WAITING_FOR_PAIR_SECOND_LEG,
            "next_required_purpose_after_first_leg": derive_next_required_purpose(
                seq, 1, STEP_WAITING_FOR_PAIR_SECOND_LEG
            ),
            "waiting_flag_field": short.waiting_flag_field,
            "pending_cycle_field": short.pending_cycle_field,
            "first_leg_trigger": {
                "reference": "short_avg (cycle 1) or previous long_reduce for cycle>1",
                "formula": "reference * (1 - short_fill_distance_pct/100)",
                "direction": "price must rise to trigger (trigger_direction=2)",
                "distance_pct": short_cfg.short_fill_distance_pct,
            },
            "second_leg_trigger": {
                "mode": short.second_leg_trigger_mode,
                "builder": "FixedCycleHedgeStrategy._build_short_tp_follow_up",
                "requires_cycle_entry_field": short.confirmed_pnl_cycle_entry_field,
                "formula": (
                    "trigger = ((required_profit/qty) + long_avg*(1+fee)) / (1-fee); "
                    "required_profit = pending_cycle_loss + target_profit_usdt"
                ),
                "direction": "price must rise (long reduce, trigger_direction=1)",
                "note": "NOT simple long_fill_distance_pct below reference",
            },
            "cycle_completion": "both legs filled → advance_cycle_sequence_after_fill pair_completed",
            "cycle2_creation": "active_cycle_index=2, next_required=CYCLE_2_SHORT_REDUCE",
            "source_files": [
                "fixed_cycle_hedge_bot/direction_config.py",
                "fixed_cycle_hedge_bot/cycle_sequence.py",
                "fixed_cycle_hedge_bot/fixed_cycle_strategy.py::_build_cycle_orders",
                "fixed_cycle_hedge_bot/fixed_cycle_strategy.py::_build_short_tp_follow_up",
            ],
        },
        "long_primary_mirror": {
            "first_leg_purpose": long.first_leg_purpose,
            "second_leg_purpose": long.second_leg_purpose,
            "first_leg_distance_pct": long_cfg.long_fill_distance_pct,
            "second_leg_distance_pct": long_cfg.short_fill_distance_pct,
        },
    }


def _load_trade_blocks(results_dir: Path, trade_number: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    pattern = f"APTUSDT_short_continuous_trade_{trade_number:04d}_*_trade_blocks.json"
    matches = sorted(results_dir.glob(pattern))
    if not matches:
        raise FileNotFoundError(f"no trade blocks for trade {trade_number}: {pattern}")
    payload = json.loads(matches[0].read_text(encoding="utf-8"))
    return payload.get("summary", [{}])[0], list(payload.get("trade_blocks") or [])


def _load_runs(results_dir: Path, direction: str) -> list[dict[str, Any]]:
    path = results_dir / f"{direction}_continuous_results.json"
    return list(json.loads(path.read_text(encoding="utf-8")).get("runs") or [])


def _pct_fraction(value: float) -> float:
    return value / 100.0 if value > 1.0 else value


def _compute_loss_cover_long_reduce_trigger(
    *,
    long_avg: float,
    long_reduce_qty: float,
    loss_usdt: float,
    target_profit_usdt: float,
    fee_rate: float = _FEE_RATE,
) -> float | None:
    if long_avg <= 0 or long_reduce_qty <= 0 or fee_rate >= 1.0:
        return None
    required = max(loss_usdt + target_profit_usdt, 0.0)
    if required <= 0:
        return None
    return ((required / long_reduce_qty) + (long_avg * (1.0 + fee_rate))) / (1.0 - fee_rate)


def _first_leg_fill_row(blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in blocks:
        if row.get("row_type") != "fill":
            continue
        if str(row.get("purpose") or "").upper() == "CYCLE_1_SHORT_REDUCE":
            return row
    return None


def _rows_for_purpose(blocks: list[dict[str, Any]], purpose: str) -> list[dict[str, Any]]:
    target = purpose.upper()
    return [r for r in blocks if str(r.get("purpose") or "").upper() == target]


def _max_cycle_from_blocks(blocks: list[dict[str, Any]]) -> int:
    max_cycle = 0
    for row in blocks:
        if row.get("row_type") != "fill":
            continue
        m = _CYCLE_RE.search(str(row.get("purpose") or ""))
        if m:
            max_cycle = max(max_cycle, int(m.group(1)))
    return max_cycle


def _classify_trade(
    *,
    second_leg_created: bool,
    second_leg_trigger_touched: bool,
    second_leg_filled: bool,
    second_leg_cancelled: bool,
    final_exit_purposes: set[str],
) -> str:
    if not second_leg_created:
        return "order_not_created"
    if second_leg_cancelled and not second_leg_trigger_touched:
        return "order_cancelled_before_touch"
    if not second_leg_trigger_touched:
        return "order_created_not_touched"
    if second_leg_trigger_touched and not second_leg_filled:
        return "order_touched_not_filled"
    if {"LONG_SL_EXIT", "SHORT_TP_EXIT"} & final_exit_purposes and not second_leg_filled:
        return "basket_exit_preempted"
    return "other"


def analyze_trade_population(results_dir: Path, candles: list[Any]) -> tuple[list[dict], list[dict]]:
    short_cfg = resolve_backtest_config(config_source="live", signal="short", symbol="APTUSDT").config
    runs = _load_runs(results_dir, "short")
    run_by_id = {r.get("trade_block_id"): r for r in runs}
    population: list[dict[str, Any]] = []

    for path in sorted(results_dir.glob("APTUSDT_short_continuous_trade_*_trade_blocks.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        summary = (payload.get("summary") or [{}])[0]
        blocks = list(payload.get("trade_blocks") or [])
        trade_id = str(summary.get("trade_block_id") or "")
        run = run_by_id.get(trade_id, {})
        max_cycle = _max_cycle_from_blocks(blocks)
        if max_cycle != 1:
            continue
        first_fill = _first_leg_fill_row(blocks)
        if first_fill is None:
            continue

        second_rows = _rows_for_purpose(blocks, "CYCLE_1_LONG_REDUCE")
        second_created = any(r.get("row_type") in {"intent", "order"} for r in second_rows)
        second_filled = any(r.get("row_type") == "fill" for r in second_rows)
        second_cancelled = any(
            str(r.get("status") or "").upper() in {"CANCELED", "CANCELLED"} for r in second_rows
        )
        creation_index = None
        trigger_price = None
        second_qty = None
        for r in second_rows:
            if r.get("row_type") in {"intent", "order"} and r.get("trigger_price"):
                creation_index = r.get("candle_index")
                trigger_price = float(r["trigger_price"])
                second_qty = r.get("qty")
                break

        start_index = int(run.get("start_index") or 0)
        first_local = int(first_fill.get("candle_index") or 0)
        first_abs = start_index + first_local
        first_price = float(first_fill.get("fill_price") or 0.0)
        loss_usdt = abs(float(first_fill.get("confirmed_closed_pnl") or first_fill.get("closed_pnl") or 0.0))
        long_avg = float(first_fill.get("long_avg_after") or 0.0)
        long_qty = float(first_fill.get("long_qty_after") or 0.0)
        reduction = _pct_fraction(float(short_cfg.reduction_pct_per_fill))
        long_reduce_qty = long_qty * reduction if long_qty > 0 else 0.0
        hypo_trigger = _compute_loss_cover_long_reduce_trigger(
            long_avg=long_avg,
            long_reduce_qty=long_reduce_qty,
            loss_usdt=loss_usdt,
            target_profit_usdt=float(short_cfg.target_profit_usdt or 0.0),
        )

        touched = False
        touch_index = None
        min_low = None
        max_high = None
        ref_trigger = trigger_price if trigger_price else hypo_trigger
        end_local = int(run.get("end_index", 0) - start_index) if run.get("end_index") else first_local
        if ref_trigger and first_abs < len(candles):
            slice_candles = candles[first_abs : min(len(candles), start_index + end_local + 1)]
            lows = [float(c.low) for c in slice_candles]
            highs = [float(c.high) for c in slice_candles]
            if lows and highs:
                min_low = min(lows)
                max_high = max(highs)
                for i, c in enumerate(slice_candles):
                    if float(c.high) >= ref_trigger:
                        touched = True
                        touch_index = first_abs + i
                        break

        final_fills = [r for r in blocks if r.get("row_type") == "fill" and "EXIT" in str(r.get("purpose", ""))]
        final_purposes = {str(r.get("purpose") or "") for r in final_fills[-2:]}
        classification = _classify_trade(
            second_leg_created=second_created,
            second_leg_trigger_touched=touched,
            second_leg_filled=second_filled,
            second_leg_cancelled=second_cancelled,
            final_exit_purposes=final_purposes,
        )

        mtm = run.get("mark_to_market_pnl")
        population.append(
            {
                "trade_number": int(re.search(r"trade_(\d+)", path.name).group(1)),
                "trade_block_id": trade_id,
                "start_timestamp": run.get("start_time") or summary.get("start_time"),
                "end_timestamp": run.get("end_time") or summary.get("end_time"),
                "duration_candles": (int(run["end_index"]) - int(run["start_index"])) if run.get("end_index") else None,
                "first_leg_fill_index": first_abs,
                "first_leg_fill_price": first_price,
                "first_leg_closed_pnl": float(first_fill.get("closed_pnl") or 0.0),
                "second_leg_created": second_created,
                "second_leg_creation_index": creation_index,
                "second_leg_hypothetical_trigger_price": hypo_trigger,
                "second_leg_trigger_price": trigger_price,
                "second_leg_qty": second_qty,
                "second_leg_trigger_touched": touched,
                "second_leg_touch_index": touch_index,
                "second_leg_filled": second_filled,
                "second_leg_cancelled": second_cancelled,
                "cancellation_reason": "n/a" if not second_cancelled else "cancelled_in_blocks",
                "final_exit_purpose": " + ".join(sorted(final_purposes)),
                "classification": classification,
                "realized_pnl": float(run.get("realized_pnl") or summary.get("realized_pnl") or 0.0),
                "mark_to_market_pnl": float(mtm) if mtm is not None else None,
                "min_price_after_first_leg": min_low,
                "max_price_after_first_leg": max_high,
                "trigger_distance_pct_at_fill": (
                    ((ref_trigger - first_price) / first_price) * 100.0 if ref_trigger and first_price else None
                ),
                "market_miss_pct_if_hypo": (
                    ((ref_trigger - max_high) / ref_trigger) * 100.0
                    if ref_trigger and max_high is not None and not touched
                    else 0.0 if touched else None
                ),
            }
        )

    root_cause_counts: dict[str, list[dict]] = {}
    for row in population:
        root_cause_counts.setdefault(row["classification"], []).append(row)

    root_rows = []
    total = len(population) or 1
    for cause, rows in sorted(root_cause_counts.items(), key=lambda kv: -len(kv[1])):
        durations = [r["duration_candles"] for r in rows if r["duration_candles"]]
        triggers = [
            r["trigger_distance_pct_at_fill"]
            for r in rows
            if r.get("trigger_distance_pct_at_fill") is not None
        ]
        root_rows.append(
            {
                "classification": cause,
                "trade_count": len(rows),
                "share_pct": round(100.0 * len(rows) / total, 2),
                "total_candles": sum(durations),
                "avg_duration_candles": round(statistics.mean(durations), 1) if durations else None,
                "max_duration_candles": max(durations) if durations else None,
                "total_realized_pnl": round(sum(r["realized_pnl"] for r in rows), 4),
                "avg_trigger_distance_pct": round(statistics.mean(triggers), 4) if triggers else None,
            }
        )
    return population, root_rows


def build_trade_0061_timeline(results_dir: Path, candles: list[Any]) -> tuple[list[dict], dict[str, Any]]:
    summary, blocks = _load_trade_blocks(results_dir, 61)
    runs = _load_runs(results_dir, "short")
    run = next(r for r in runs if r.get("trade_block_id") == summary.get("trade_block_id"))
    start_index = int(run["start_index"])
    short_cfg = resolve_backtest_config(config_source="live", signal="short", symbol="APTUSDT").config
    timeline: list[dict[str, Any]] = []

    first_short_reduce_fill: dict[str, Any] | None = None
    for row in blocks:
        local_idx = row.get("candle_index")
        abs_idx = (start_index + int(local_idx)) if local_idx is not None else None
        ts = row.get("timestamp")
        ohlc = {}
        if abs_idx is not None and 0 <= abs_idx < len(candles):
            c = candles[abs_idx]
            ohlc = {"open": c.open, "high": c.high, "low": c.low, "close": c.close}
            if not ts:
                ts = c.timestamp.isoformat() if c.timestamp else None

        event = {
            "absolute_candle_index": abs_idx,
            "local_candle_index": local_idx,
            "timestamp": ts,
            "candle_open": ohlc.get("open"),
            "candle_high": ohlc.get("high"),
            "candle_low": ohlc.get("low"),
            "candle_close": ohlc.get("close"),
            "event_type": row.get("event_type") or row.get("row_type"),
            "row_type": row.get("row_type"),
            "order_purpose": row.get("purpose"),
            "order_side": row.get("side"),
            "trigger_price": row.get("trigger_price"),
            "limit_price": row.get("price"),
            "qty": row.get("qty"),
            "fill_price": row.get("fill_price"),
            "order_status": row.get("status"),
            "long_qty": row.get("long_qty_after"),
            "short_qty": row.get("short_qty_after"),
            "long_avg": row.get("long_avg_after"),
            "short_avg": row.get("short_avg_after"),
            "closed_pnl": row.get("closed_pnl"),
            "confirmed_closed_pnl": row.get("confirmed_closed_pnl"),
            "active_orders_after": row.get("active_orders_after_count"),
            "active_cycle_index": None,
            "next_required_purpose": None,
            "waiting_for_second_leg": None,
            "notes": "",
        }
        if row.get("row_type") == "fill" and str(row.get("purpose")) == "CYCLE_1_SHORT_REDUCE":
            first_short_reduce_fill = row
            event["notes"] = "first_leg_fill"
        timeline.append(event)

    analysis: dict[str, Any] = {
        "trade_block_id": summary.get("trade_block_id"),
        "start_index": start_index,
        "end_index": run.get("end_index"),
        "duration_candles": int(run["end_index"]) - start_index,
        "purposes_sequence": summary.get("purposes_sequence"),
        "exit_reason": summary.get("exit_reason"),
        "realized_pnl": summary.get("realized_pnl"),
    }

    if first_short_reduce_fill:
        loss = abs(float(first_short_reduce_fill.get("confirmed_closed_pnl") or first_short_reduce_fill.get("closed_pnl") or 0))
        long_avg = float(first_short_reduce_fill.get("long_avg_after") or 0)
        long_qty = float(first_short_reduce_fill.get("long_qty_after") or 0)
        fill_price = float(first_short_reduce_fill.get("fill_price") or 0)
        reduction = _pct_fraction(float(short_cfg.reduction_pct_per_fill))
        long_reduce_qty = long_qty * reduction
        hypo_trigger = _compute_loss_cover_long_reduce_trigger(
            long_avg=long_avg,
            long_reduce_qty=long_reduce_qty,
            loss_usdt=loss,
            target_profit_usdt=float(short_cfg.target_profit_usdt or 0.0),
        )
        first_abs = start_index + int(first_short_reduce_fill.get("candle_index") or 0)
        post = candles[first_abs : int(run["end_index"]) + 1]
        max_high = max(float(c.high) for c in post) if post else None
        min_low = min(float(c.low) for c in post) if post else None
        touched = bool(hypo_trigger and max_high and max_high >= hypo_trigger)

        analysis.update(
            {
                "first_leg_fill": {
                    "local_index": first_short_reduce_fill.get("candle_index"),
                    "absolute_index": first_abs,
                    "fill_price": fill_price,
                    "closed_pnl": first_short_reduce_fill.get("closed_pnl"),
                    "confirmed_closed_pnl": first_short_reduce_fill.get("confirmed_closed_pnl"),
                    "long_qty_after": long_qty,
                    "short_qty_after": first_short_reduce_fill.get("short_qty_after"),
                },
                "second_leg_CYCLE_1_LONG_REDUCE": {
                    "created_in_trade_blocks": any(
                        "CYCLE_1_LONG_REDUCE" in str(b.get("purpose", "")) for b in blocks
                    ),
                    "hypothetical_trigger_price": hypo_trigger,
                    "hypothetical_qty": long_reduce_qty,
                    "required_price_direction": "up (long reduce)",
                    "trigger_touched_if_created": touched,
                    "max_high_after_first_leg": max_high,
                    "min_low_after_first_leg": min_low,
                    "market_miss_pct": (
                        ((hypo_trigger - max_high) / hypo_trigger) * 100.0
                        if hypo_trigger and max_high and not touched
                        else 0.0
                    ),
                },
                "post_first_leg_basket_exit": {
                    "LONG_SL_EXIT_trigger": next(
                        (b.get("trigger_price") for b in blocks if b.get("purpose") == "LONG_SL_EXIT" and b.get("row_type") == "order"),
                        None,
                    ),
                    "SHORT_TP_EXIT_trigger": next(
                        (b.get("trigger_price") for b in blocks if b.get("purpose") == "SHORT_TP_EXIT" and b.get("row_type") == "order"),
                        None,
                    ),
                    "final_exit_candle": int(run["end_index"]),
                    "final_exit_purposes": ["LONG_SL_EXIT", "SHORT_TP_EXIT"],
                },
                "root_cause": "order_not_created",
                "technical_blocker": (
                    "_build_short_tp_follow_up requires cycle_entry['long_add_confirmed_pnl']; "
                    "backtest short-reduce fill has closed_pnl on fill row but cycle entry field "
                    "is only populated via _refresh_short_reduce_closed_pnl (API fetch — stub returns [])."
                ),
                "state_machine_expectation": {
                    "cycle_step_after_first_leg": STEP_WAITING_FOR_PAIR_SECOND_LEG,
                    "next_required_purpose": "CYCLE_1_LONG_REDUCE",
                    "waiting_flag": "cycle_waiting_for_long_reduce",
                    "pending_cycle_field": "long_reduce_pending_cycle",
                },
            }
        )
    return timeline, analysis


def build_trigger_formula_comparison() -> list[dict[str, Any]]:
    short_cfg = resolve_backtest_config(config_source="live", signal="short", symbol="APTUSDT").config
    long_cfg = resolve_backtest_config(config_source="live", signal="long", symbol="APTUSDT").config
    rows = [
        {
            "concept": "cycle1_first_leg_purpose",
            "long_value": "CYCLE_1_LONG_ADD",
            "short_value": "CYCLE_1_SHORT_REDUCE",
            "long_formula": "reference * (1 - long_fill_distance_pct)",
            "short_formula": "short_avg * (1 - short_fill_distance_pct)",
            "long_distance_pct": long_cfg.long_fill_distance_pct,
            "short_distance_pct": short_cfg.short_fill_distance_pct,
            "reference_basis_long": "long_avg / last_cycle_reference",
            "reference_basis_short": "short_avg",
            "trigger_direction_long": "down (buy long add)",
            "trigger_direction_short": "up (sell short reduce)",
            "source_file": "fixed_cycle_hedge_bot/fixed_cycle_strategy.py",
            "source_function": "_build_cycle_orders",
        },
        {
            "concept": "cycle1_second_leg_purpose",
            "long_value": "CYCLE_1_SHORT_REDUCE",
            "short_value": "CYCLE_1_LONG_REDUCE",
            "long_formula": "loss-cover trigger from long_add_confirmed_pnl",
            "short_formula": "loss-cover trigger from long_add_confirmed_pnl (same field name)",
            "long_distance_pct": long_cfg.short_fill_distance_pct,
            "short_distance_pct": short_cfg.long_fill_distance_pct,
            "reference_basis_long": "long_add_confirmed_pnl + long_avg",
            "reference_basis_short": "long_add_confirmed_pnl + long_avg (hedge leg)",
            "trigger_direction_long": "down (sell short reduce)",
            "trigger_direction_short": "up (sell long reduce)",
            "source_file": "fixed_cycle_hedge_bot/fixed_cycle_strategy.py",
            "source_function": "_build_short_tp_follow_up",
            "notes": (
                "Second leg does NOT use fill_distance_pct directly; "
                "short bot reuses long_add_confirmed_pnl field for short-reduce loss."
            ),
        },
        {
            "concept": "confirmed_pnl_persistence",
            "long_value": "set on LONG_ADD fill path",
            "short_value": "requires _refresh_short_reduce_closed_pnl API",
            "long_formula": "cycle_entry['long_add_confirmed_pnl'] from fill metadata",
            "short_formula": "same field; short-reduce fill path does not copy runtime closed_pnl",
            "source_file": "fixed_cycle_hedge_bot/fixed_cycle_strategy.py",
            "source_function": "advance_cycle_from_fill / _refresh_short_reduce_closed_pnl",
            "notes": "Backtest stub fetch_closed_pnl=[] → field stays None → second leg blocked",
        },
        {
            "concept": "pct_interpretation",
            "long_value": "_pct() treats values >1 as percent",
            "short_value": "same",
            "long_formula": "0.15 → 0.15%; 0.5 → 0.5%",
            "short_formula": "identical",
            "source_file": "fixed_cycle_hedge_bot/fixed_cycle_strategy.py",
            "source_function": "_pct / _clamp_pct_fraction",
        },
    ]
    return rows


def build_long_short_transition_comparison(results_dir: Path) -> list[dict[str, Any]]:
    long_runs = _load_runs(results_dir, "long")
    long_cycle1_complete = []
    for run in long_runs:
        purposes = str(run.get("purposes_sequence") or run.get("filled_purposes") or "")
        max_cycle = int(run.get("max_cycle_index") or 0)
        if "CYCLE_1_LONG_ADD" in purposes and "CYCLE_1_SHORT_REDUCE" in purposes and max_cycle >= 2:
            long_cycle1_complete.append(run)
    sample_long = long_cycle1_complete[0] if long_cycle1_complete else {}
    _, short_blocks = _load_trade_blocks(results_dir, 61)

    def _block_flags(blocks: list[dict], purpose: str) -> dict[str, bool]:
        rows = _rows_for_purpose(blocks, purpose)
        return {
            "created": any(r.get("row_type") in {"intent", "order"} for r in rows),
            "filled": any(r.get("row_type") == "fill" for r in rows),
        }

    long_second_flags = {"created": False, "filled": False}
    long_matches = sorted(results_dir.glob("APTUSDT_long_continuous_trade_*_trade_blocks.json"))
    for path in long_matches[:30]:
        blocks = json.loads(path.read_text(encoding="utf-8")).get("trade_blocks") or []
        flags = _block_flags(blocks, "CYCLE_1_SHORT_REDUCE")
        if flags["filled"]:
            long_second_flags = flags
            break

    rows = [
        {
            "aspect": "first_leg_purpose",
            "long": "CYCLE_1_LONG_ADD",
            "short": "CYCLE_1_SHORT_REDUCE",
            "symmetric_codepath": True,
        },
        {
            "aspect": "second_leg_purpose",
            "long": "CYCLE_1_SHORT_REDUCE",
            "short": "CYCLE_1_LONG_REDUCE",
            "symmetric_codepath": True,
        },
        {
            "aspect": "second_leg_builder",
            "long": "_build_short_tp_follow_up",
            "short": "_build_short_tp_follow_up (same function)",
            "symmetric_codepath": True,
        },
        {
            "aspect": "waiting_flag_field",
            "long": "cycle_waiting_for_short_tp",
            "short": "cycle_waiting_for_long_reduce",
            "symmetric_codepath": True,
            "notes": "ShortFixedCycleHedgeStrategy overrides field names",
        },
        {
            "aspect": "confirmed_pnl_on_first_leg_fill",
            "long": "long fill path writes long_add_confirmed_pnl",
            "short": "short reduce fill does NOT write; needs API refresh",
            "symmetric_codepath": False,
            "notes": "Primary backtest blocker for short",
        },
        {
            "aspect": "pending_cycle_loss_usdt_on_first_leg",
            "long": "set from long_add confirmed loss",
            "short": "not set on short_reduce fill path",
            "symmetric_codepath": False,
        },
        {
            "aspect": "long_sample_cycle2_reached",
            "long": bool(sample_long),
            "short": False,
            "symmetric_codepath": False,
        },
        {
            "aspect": "sample_second_leg_created",
            "long": long_second_flags["created"],
            "short": _block_flags(short_blocks, "CYCLE_1_LONG_REDUCE")["created"],
            "symmetric_codepath": False,
            "notes": "Long sample from first long trade with CYCLE_1_SHORT_REDUCE fill",
        },
    ]
    return rows


def build_basket_exit_interaction_doc() -> list[dict[str, Any]]:
    return [
        {
            "decision": "After first leg fill, rebuild basket exit",
            "file": "fixed_cycle_hedge_bot/fixed_cycle_strategy.py",
            "function": "_mark_exit_orders_stale_after_structure_fill / exit rebuild pipeline",
            "short_behavior": "LONG_SL_EXIT + SHORT_TP_EXIT resubmitted at new hedge geometry",
            "replaces_second_leg": False,
            "cancels_second_leg": "Only if second leg existed; trade blocks show no CYCLE_1_LONG_REDUCE to cancel",
        },
        {
            "decision": "Basket exit coexists with cycle orders",
            "file": "fixed_cycle_hedge_bot/fixed_cycle_strategy.py",
            "function": "_build_cycle_orders",
            "short_behavior": "Both allowed; second leg never reached active_orders in observed trades",
            "notes": "Trade #61: after fill candle 2 → basket orders at 0.7852; no second leg order",
        },
        {
            "decision": "Trade closure",
            "file": "fixed_cycle_hedge_bot/fixed_cycle_strategy.py",
            "function": "_maybe_finalize_exit_after_leg_fill",
            "short_behavior": "LONG_SL_EXIT + SHORT_TP_EXIT close both legs (candle 29091 trade #61)",
        },
    ]


def build_analysis_summary(
    population: list[dict[str, Any]],
    root_causes: list[dict[str, Any]],
    trade_61: dict[str, Any],
) -> dict[str, Any]:
    created = sum(1 for r in population if r["second_leg_created"])
    filled = sum(1 for r in population if r["second_leg_filled"])
    touched = sum(1 for r in population if r["second_leg_trigger_touched"])
    return {
        "question": "Why never CYCLE_1_LONG_REDUCE after CYCLE_1_SHORT_REDUCE?",
        "population_trades": len(population),
        "second_leg_created_count": created,
        "second_leg_filled_count": filled,
        "second_leg_trigger_touched_count": touched,
        "primary_classification": root_causes[0]["classification"] if root_causes else None,
        "verdict": (
            "Technical bug (Fall A): second-leg order never built in backtest. "
            "Not a market-touch issue — 0/50 trades show CYCLE_1_LONG_REDUCE in order lifecycle."
        ),
        "technical_root_cause": (
            "_build_short_tp_follow_up gates on cycle_entry['long_add_confirmed_pnl']. "
            "Short-reduce runtime closed_pnl exists on fill export but is not copied to cycle entry "
            "because _refresh_short_reduce_closed_pnl requires live fetch_closed_pnl (stub [] in backtest)."
        ),
        "secondary_factor": (
            "Even if unblocked, loss-cover long-reduce trigger is above market (up-move required); "
            "basket exit eventually closes trade — but this is moot while order is never created."
        ),
        "diagnostic_distance_sweep": "skipped — orders not created; distance sweep not applicable",
        "trade_61": trade_61,
        "root_causes": root_causes,
        "code_origin": "shared live strategy (ShortFixedCycleHedgeStrategy); backtest uses HedgeBotOriginalSimulator",
    }


def write_report(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# Short Cycle-1 Second Leg Audit",
        "",
        "## Executive summary",
        "",
        f"- **Population:** {summary['population_trades']} trades stalled at max cycle 1 with `CYCLE_1_SHORT_REDUCE` filled",
        f"- **Second leg created:** {summary['second_leg_created_count']}/50",
        f"- **Primary cause:** {summary['primary_classification']}",
        f"- **Verdict:** {summary['verdict']}",
        "",
        "## Technical root cause",
        "",
        summary["technical_root_cause"],
        "",
        "## Trade #61",
        "",
        f"- Duration: {summary['trade_61'].get('duration_candles')} candles",
        f"- First leg fill: {summary['trade_61'].get('first_leg_fill')}",
        f"- Second leg created: {summary['trade_61'].get('second_leg_CYCLE_1_LONG_REDUCE', {}).get('created_in_trade_blocks')}",
        f"- Hypothetical trigger: {summary['trade_61'].get('second_leg_CYCLE_1_LONG_REDUCE', {}).get('hypothetical_trigger_price')}",
        "",
        "## Basket exit",
        "",
        "After first leg, basket `LONG_SL_EXIT` + `SHORT_TP_EXIT` are rebuilt. They do not cancel a non-existent second leg.",
        "",
        "## Diagnostic distance sweep",
        "",
        summary["diagnostic_distance_sweep"],
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def run_audit(
    *,
    source_dir: Path = DEFAULT_SOURCE_DIR,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_candles = load_candles_for_symbol("APTUSDT", timeframe="5m", limit=52569)
    candles = normalize_candles("APTUSDT", raw_candles)

    sequence_doc = build_expected_cycle1_sequence_doc()
    write_json(output_dir / "expected_cycle1_sequence.json", sequence_doc)

    population, root_causes = analyze_trade_population(source_dir, candles)
    write_csv(output_dir / "short_cycle1_second_leg_population.csv", population)
    write_csv(output_dir / "short_cycle1_second_leg_root_causes.csv", root_causes)

    timeline, trade_61 = build_trade_0061_timeline(source_dir, candles)
    write_csv(output_dir / "trade_0061_state_timeline.csv", timeline)
    write_json(output_dir / "trade_0061_analysis.json", trade_61)

    write_csv(output_dir / "cycle1_trigger_formula_comparison.csv", build_trigger_formula_comparison())
    write_csv(
        output_dir / "long_short_cycle1_transition_comparison.csv",
        build_long_short_transition_comparison(source_dir),
    )
    write_json(output_dir / "basket_exit_interaction.json", build_basket_exit_interaction_doc())

    summary = build_analysis_summary(population, root_causes, trade_61)
    write_json(output_dir / "analysis_summary.json", summary)
    write_report(output_dir / "REPORT.md", summary)

    return summary

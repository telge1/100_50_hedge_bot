"""Research helpers for L0/L1 long-primary baseline notional + stage-TP audit."""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from typing import Any

from research.backtests.backtest_report import BacktestResult
from research.backtests.inventory_mtm_freeze import is_injusdt_trade8_undercoverage
from research.backtests.long_add_multistart_metrics import (
    analyze_trade,
    exposure_from_fills,
    normalize_trade_status,
    safe_float,
)
from research.backtests.safe_cycle_boundary_freeze import detect_invalid_partial_cycle

L0_LONG_NOTIONAL = 100.0
L0_SHORT_NOTIONAL = 50.0
L1_LONG_NOTIONAL = 1000.0
L1_SHORT_NOTIONAL = 500.0

# Parity targets from current_baseline_multicoin_continuous_blocker_audit_20260720.
L0_REFERENCE_TRADES = 265
L0_REFERENCE_CLOSED = 238
L0_REFERENCE_BLOCKERS = 27
L0_REFERENCE_CLOSED_PNL = 60.70230719517889
L0_REFERENCE_SERIES_MTM = -291.96557591506945
L0_REFERENCE_BLOCKER_MTM = -352.66788211024834
L0_MTM_TOLERANCE = 0.5
L0_CLOSED_PNL_TOLERANCE = 0.01

CYCLE_SECOND_LEG_RE = re.compile(r"^CYCLE_(\d+)_(SHORT_REDUCE|SHORT_TP|LONG_REDUCE)$")


def _ts(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def build_baseline_call_kwargs(
    *,
    symbol: str,
    candles: list[Any],
    base_notional_usdt: float,
) -> dict[str, Any]:
    """Clean baseline kwargs — no freeze, no recovery, no exit-rebuild policy."""
    return {
        "symbol": symbol,
        "direction": "long",
        "candles": candles,
        "continuous_start_index": 0,
        "config_source": "live",
        "fill_model": "conservative",
        "tp_profit_target_pct": 0.25,
        "long_fill_distance_pct": 0.5,
        "target_profit_usdt": 0.015,
        "base_notional_usdt": float(base_notional_usdt),
        "initial_notional_usdt": float(base_notional_usdt),
        "write_json": False,
        "write_csv": False,
    }


def _cycle_from_purpose(purpose: str) -> int | None:
    match = CYCLE_SECOND_LEG_RE.match(str(purpose or ""))
    return int(match.group(1)) if match else None


def _is_stage_intent(meta: dict[str, Any]) -> bool:
    if meta.get("is_staged_second_leg_tp") and meta.get("stage_index") is not None:
        return True
    if meta.get("normal_cycle_second_leg_split") and meta.get("split_stage_index") is not None:
        return True
    return False


def _stage_index(meta: dict[str, Any]) -> int | None:
    if meta.get("stage_index") is not None:
        return int(safe_float(meta.get("stage_index"), -1))
    if meta.get("split_stage_index") is not None:
        return int(safe_float(meta.get("split_stage_index"), -1))
    return None


def extract_stage_tp_attempts(
    *,
    coin: str,
    variant: str,
    trade_number: int,
    result: BacktestResult,
    exchange_min_notional: float = 5.0,
    exchange_min_qty: float = 0.0,
    qty_step: float = 0.0,
) -> list[dict[str, Any]]:
    """One row per staged/split second-leg build attempt (grouped by cycle)."""
    stage_by_cycle: dict[int, list[dict[str, Any]]] = defaultdict(list)
    fallback_by_cycle: dict[int, dict[str, Any]] = {}

    for intent in result.intent_log or []:
        purpose = str(intent.get("purpose") or "")
        cycle = _cycle_from_purpose(purpose)
        if cycle is None:
            continue
        meta = dict(intent.get("metadata_excerpt") or {})
        if _is_stage_intent(meta):
            stage_by_cycle[cycle].append(intent)
        elif meta.get("fallback_to_single_second_leg"):
            fallback_by_cycle[cycle] = intent

    rows: list[dict[str, Any]] = []
    all_cycles = sorted(set(stage_by_cycle.keys()) | set(fallback_by_cycle.keys()))
    fills = list(result.fill_log or [])

    for cycle in all_cycles:
        stages = sorted(stage_by_cycle.get(cycle, []), key=lambda i: _stage_index(i.get("metadata_excerpt") or {}) or 0)
        fallback = fallback_by_cycle.get(cycle)
        timestamp = _ts((stages[0] if stages else fallback or {}).get("timestamp"))
        side = str((stages[0] if stages else fallback or {}).get("side") or "")
        purpose = str((stages[0] if stages else fallback or {}).get("purpose") or "")

        stage_qtys = [safe_float(s.get("qty")) for s in stages]
        stage_prices = [safe_float(s.get("trigger_price")) for s in stages]
        stage_notionals = [
            (q or 0.0) * (p or 0.0) for q, p in zip(stage_qtys, stage_prices)
        ]
        total_qty = safe_float((stages[0] if stages else fallback or {}).get("qty"))
        if stages:
            meta0 = stages[0].get("metadata_excerpt") or {}
            total_qty = safe_float(meta0.get("split_total_qty")) or sum(q or 0.0 for q in stage_qtys) or total_qty

        fb_meta = (fallback or {}).get("metadata_excerpt") or {}
        rejected_count = int(safe_float(fb_meta.get("rejected_stage_count"), 0) or 0)
        rejected_notionals = fb_meta.get("rejected_stage_notional_values") or fb_meta.get(
            "rejected_stage_notionals"
        ) or []
        original_stage_count = int(
            safe_float(fb_meta.get("original_stage_count") or (stages[0].get("metadata_excerpt") or {}).get("stage_count"), 0)
            or 0
        )
        full_fallback = bool(fallback and not stages)

        cycle_fills = [
            f
            for f in fills
            if _cycle_from_purpose(str(f.get("purpose") or "")) == cycle
            and str(f.get("purpose") or "").endswith(("_SHORT_REDUCE", "_SHORT_TP", "_LONG_REDUCE"))
        ]
        stage_fill_count = len(cycle_fills) if stages else (1 if cycle_fills and full_fallback else len(cycle_fills))
        realized_stage_pnl = sum(safe_float(f.get("closed_pnl") or f.get("confirmed_closed_pnl")) for f in cycle_fills)

        if stages:
            accepted = 1
            rejected = 0
            rejection_reason = ""
            actual_stage_count = len(stages)
        elif full_fallback:
            accepted = 0
            rejected = 1
            rejection_reason = str(fb_meta.get("split_fallback_reason") or "stage_below_min_notional")
            actual_stage_count = 0
        else:
            continue

        proposed_stage_qty = stage_qtys[0] if stage_qtys else total_qty
        proposed_stage_notional = stage_notionals[0] if stage_notionals else None
        if full_fallback and rejected_notionals:
            proposed_stage_notional = safe_float(rejected_notionals[0])

        rows.append(
            {
                "coin": coin,
                "variant": variant,
                "trade_id": trade_number,
                "cycle": cycle,
                "timestamp": timestamp,
                "side": side,
                "purpose": purpose,
                "total_second_leg_qty": total_qty,
                "proposed_stage_qty": proposed_stage_qty,
                "proposed_stage_notional": proposed_stage_notional,
                "exchange_min_notional": exchange_min_notional,
                "exchange_min_qty": exchange_min_qty,
                "qty_step": qty_step,
                "accepted": accepted,
                "rejected": rejected,
                "rejection_reason": rejection_reason,
                "actual_stage_count": actual_stage_count,
                "actual_stage_qtys": stage_qtys if stages else [],
                "actual_stage_notionals": stage_notionals if stages else list(rejected_notionals),
                "stage_fill_count": stage_fill_count,
                "full_second_leg_fallback_used": int(full_fallback),
                "realized_stage_pnl": realized_stage_pnl,
                "original_stage_count": original_stage_count,
                "rejected_stage_count": rejected_count,
            }
        )
    return rows


def extract_stage_tp_fills(
    *,
    coin: str,
    variant: str,
    trade_number: int,
    result: BacktestResult,
) -> list[dict[str, Any]]:
    """Fill rows for second-leg purposes that may be stage/split partial TPs."""
    rows: list[dict[str, Any]] = []
    for fill in result.fill_log or []:
        purpose = str(fill.get("purpose") or "")
        cycle = _cycle_from_purpose(purpose)
        if cycle is None:
            continue
        rows.append(
            {
                "coin": coin,
                "variant": variant,
                "trade_id": trade_number,
                "cycle": cycle,
                "timestamp": _ts(fill.get("timestamp")),
                "candle_index": fill.get("candle_index"),
                "purpose": purpose,
                "side": fill.get("side"),
                "qty": safe_float(fill.get("qty")),
                "fill_price": safe_float(fill.get("fill_price")),
                "closed_pnl": safe_float(fill.get("closed_pnl") or fill.get("confirmed_closed_pnl")),
                "is_partial_second_leg": int(len([f for f in result.fill_log or [] if _cycle_from_purpose(str(f.get("purpose") or "")) == cycle and str(f.get("purpose") or "").endswith(("_SHORT_REDUCE", "_SHORT_TP", "_LONG_REDUCE"))]) > 1),
            }
        )
    return rows


def min_notional_rejection_rows(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in attempts
        if row.get("rejected")
        and "min_notional" in str(row.get("rejection_reason") or "").lower()
    ]


def trade_row_from_result(
    *,
    coin: str,
    variant: str,
    result: BacktestResult,
    candles: list[Any],
    long_notional: float,
    long_add_pct: float = 0.5,
    target_profit_usdt: float = 0.015,
) -> dict[str, Any]:
    start_index = int(result.start_index or 0)
    window = candles[start_index:]
    analysis = analyze_trade(
        result,
        variant=variant,
        long_add_pct=long_add_pct,
        target_profit_usdt=target_profit_usdt,
        window_candles=window,
        valid=True,
        skip_reason="ok",
    )
    status = normalize_trade_status(result)
    excerpt = dict(result.final_strategy_state_excerpt or {})
    strategy_excerpt = dict(excerpt.get("strategy_state") or excerpt)
    invalid_partial = int(
        detect_invalid_partial_cycle(strategy_excerpt) if status != "closed" else False
    )
    fills = list(result.fill_log or [])
    exposure = exposure_from_fills(fills)
    realized = safe_float(analysis.get("realized_pnl"))
    mtm = safe_float(analysis.get("mtm_pnl"))

    return {
        "coin": coin,
        "variant": variant,
        "trade_number": int(result.trade_number or 0),
        "start_index": start_index,
        "end_index": result.end_index,
        "start_timestamp": _ts(result.start_time),
        "end_timestamp": _ts(result.end_time),
        "status": status,
        "is_blocker": int(status != "closed"),
        "duration_candles": int(result.candles_processed or 0),
        "realized_pnl": realized,
        "unrealized_pnl": analysis.get("unrealized_pnl"),
        "mtm_pnl": mtm,
        "closed_pnl_usdt": realized if status == "closed" else 0.0,
        "final_open_mtm_usdt": mtm if status != "closed" else 0.0,
        "max_cycle": analysis.get("max_cycle"),
        "completed_cycles": analysis.get("completed_cycles"),
        "undercoverage": analysis.get("undercoverage"),
        "pending_final_exit": analysis.get("pending_final_exit"),
        "invalid_partial_cycle": invalid_partial,
        "exit_rebuild_count": analysis.get("exit_rebuild_count"),
        "exit_increase_count": analysis.get("exit_increase_count"),
        "max_total_notional": analysis.get("max_total_notional"),
        "max_abs_net_exposure": analysis.get("max_abs_net_exposure"),
        "fees": analysis.get("fees"),
        "initial_long_notional_usdt": long_notional,
        "exit_reason": result.exit_reason,
        "injusdt_trade8_marker": int(
            is_injusdt_trade8_undercoverage(coin=coin, trade_number=int(result.trade_number or 0))
        ),
        "fill_log": fills,
        "intent_log": list(result.intent_log or []),
        "worst_mtm": mtm,
    }


def summarize_variant(
    rows: list[dict[str, Any]],
    *,
    variant: str,
    long_notional: float,
    short_notional: float,
) -> dict[str, Any]:
    closed = [r for r in rows if not r.get("is_blocker")]
    open_rows = [r for r in rows if r.get("is_blocker")]
    closed_pnls = [safe_float(r.get("closed_pnl_usdt")) for r in closed]
    durations = [int(r.get("duration_candles") or 0) for r in rows]
    cycles = [int(safe_float(r.get("max_cycle")) or 0) for r in rows]
    open_mtms = [safe_float(r.get("final_open_mtm_usdt") or r.get("mtm_pnl")) for r in open_rows]
    gross_exposures = [safe_float(r.get("max_total_notional")) for r in rows]
    net_exposures = [safe_float(r.get("max_abs_net_exposure")) for r in rows]
    closed_pnl = sum(closed_pnls)
    open_mtm = sum(open_mtms)
    total = sum(safe_float(r.get("mtm_pnl")) for r in rows)
    return {
        "variant": variant,
        "initial_long_notional_usdt": long_notional,
        "initial_short_notional_usdt": short_notional,
        "trades_started": len(rows),
        "trades_closed": len(closed),
        "closed_positive_count": sum(1 for p in closed_pnls if p > 1e-9),
        "closed_negative_count": sum(1 for p in closed_pnls if p < -1e-9),
        "open_blocker_count": len(open_rows),
        "closed_rate": (len(closed) / len(rows)) if rows else 0.0,
        "closed_pnl_usdt": closed_pnl,
        "final_open_mtm_usdt": open_mtm,
        "total_series_mtm_usdt": total,
        "avg_closed_pnl": statistics.fmean(closed_pnls) if closed_pnls else 0.0,
        "median_closed_pnl": float(statistics.median(closed_pnls)) if closed_pnls else None,
        "avg_trade_duration_candles": statistics.fmean(durations) if durations else 0.0,
        "median_trade_duration_candles": float(statistics.median(durations)) if durations else None,
        "max_trade_duration_candles": max(durations) if durations else 0,
        "cycle_distribution": dict(sorted(Counter(cycles).items())),
        "avg_highest_cycle": statistics.fmean(cycles) if cycles else 0.0,
        "maximum_cycle_reached": max(cycles) if cycles else 0,
        "exit_rebuild_total": sum(int(r.get("exit_rebuild_count") or 0) for r in rows),
        "exit_increase_total": sum(int(r.get("exit_increase_count") or 0) for r in rows),
        "avg_gross_exposure": statistics.fmean(gross_exposures) if gross_exposures else 0.0,
        "max_gross_exposure": max(gross_exposures) if gross_exposures else 0.0,
        "avg_net_exposure": statistics.fmean(net_exposures) if net_exposures else 0.0,
        "max_net_exposure": max(net_exposures) if net_exposures else 0.0,
        "worst_mtm_per_trade": min(safe_float(r.get("mtm_pnl")) for r in rows) if rows else None,
        "undercoverage_count": sum(int(safe_float(r.get("undercoverage")) or 0) for r in rows),
        "invalid_partial_cycle_count": sum(int(r.get("invalid_partial_cycle") or 0) for r in rows),
        "pending_final_exit_count": sum(int(safe_float(r.get("pending_final_exit")) or 0) for r in rows),
        "max_single_blocker_loss": min(open_mtms) if open_mtms else 0.0,
        "total_fees": sum(safe_float(r.get("fees")) for r in rows),
    }


def summarize_stage_comparison(
    l0_attempts: list[dict[str, Any]],
    l1_attempts: list[dict[str, Any]],
    l0_fills: list[dict[str, Any]],
    l1_fills: list[dict[str, Any]],
) -> dict[str, Any]:
    def _stats(attempts: list[dict[str, Any]], fills: list[dict[str, Any]]) -> dict[str, Any]:
        rejected_min = sum(
            1
            for a in attempts
            if a.get("rejected") and "min_notional" in str(a.get("rejection_reason") or "").lower()
        )
        accepted = sum(1 for a in attempts if a.get("accepted"))
        fallbacks = sum(1 for a in attempts if a.get("full_second_leg_fallback_used"))
        partial_fills = sum(1 for f in fills if f.get("is_partial_second_leg"))
        cycles_with_partial = len({(f["coin"], f["trade_id"], f["cycle"]) for f in fills if f.get("is_partial_second_leg")})
        stage_pnl = sum(safe_float(a.get("realized_stage_pnl")) for a in attempts)
        avg_fills = (
            statistics.fmean([int(a.get("stage_fill_count") or 0) for a in attempts if a.get("accepted")])
            if any(a.get("accepted") for a in attempts)
            else 0.0
        )
        return {
            "staged_order_attempts": len(attempts),
            "staged_orders_accepted": accepted,
            "staged_orders_rejected_min_notional": rejected_min,
            "full_qty_fallback_count": fallbacks,
            "partial_tp_fills": partial_fills,
            "cycles_with_active_partial_tp": cycles_with_partial,
            "avg_stage_fills_per_accepted_cycle": avg_fills,
            "realized_stage_pnl_usdt": stage_pnl,
        }

    l0s = _stats(l0_attempts, l0_fills)
    l1s = _stats(l1_attempts, l1_fills)
    return {"L0": l0s, "L1": l1s}


def check_l0_parity(summary: dict[str, Any]) -> dict[str, Any]:
    checks = {
        "trades": (
            summary.get("trades_started"),
            L0_REFERENCE_TRADES,
            summary.get("trades_started") == L0_REFERENCE_TRADES,
        ),
        "closed": (
            summary.get("trades_closed"),
            L0_REFERENCE_CLOSED,
            summary.get("trades_closed") == L0_REFERENCE_CLOSED,
        ),
        "blockers": (
            summary.get("open_blocker_count"),
            L0_REFERENCE_BLOCKERS,
            summary.get("open_blocker_count") == L0_REFERENCE_BLOCKERS,
        ),
        "closed_pnl": (
            summary.get("closed_pnl_usdt"),
            L0_REFERENCE_CLOSED_PNL,
            abs(safe_float(summary.get("closed_pnl_usdt")) - L0_REFERENCE_CLOSED_PNL)
            <= L0_CLOSED_PNL_TOLERANCE,
        ),
        "series_mtm": (
            summary.get("total_series_mtm_usdt"),
            L0_REFERENCE_SERIES_MTM,
            abs(safe_float(summary.get("total_series_mtm_usdt")) - L0_REFERENCE_SERIES_MTM)
            <= L0_MTM_TOLERANCE,
        ),
        "invalid_partial": (
            summary.get("invalid_partial_cycle_count"),
            0,
            int(summary.get("invalid_partial_cycle_count") or 0) == 0,
        ),
    }
    return {"ok": all(c[2] for c in checks.values()), "checks": checks}


def start_parity_row(
    *,
    coin: str,
    candles: list[Any],
    l0_first: dict[str, Any] | None,
    l1_first: dict[str, Any] | None,
) -> dict[str, Any]:
    c0 = candles[0] if candles else None
    mark = float(c0.close) if c0 is not None else None
    ts = _ts(getattr(c0, "timestamp", None)) if c0 is not None else ""
    l0_idx = int(l0_first.get("start_index")) if l0_first and l0_first.get("start_index") is not None else None
    l1_idx = int(l1_first.get("start_index")) if l1_first and l1_first.get("start_index") is not None else None
    return {
        "coin": coin,
        "first_entry_index_L0": l0_idx,
        "first_entry_index_L1": l1_idx,
        "first_entry_timestamp_L0": (l0_first or {}).get("start_timestamp") or ts,
        "first_entry_timestamp_L1": (l1_first or {}).get("start_timestamp") or ts,
        "first_entry_mark_L0": mark,
        "first_entry_mark_L1": mark,
        "start_parity_pass": int(l0_idx == l1_idx),
    }


def _fill_signature(fill: dict[str, Any]) -> tuple[Any, ...]:
    return (
        fill.get("candle_index"),
        str(fill.get("purpose") or ""),
        round(safe_float(fill.get("qty")) or 0.0, 6),
        round(safe_float(fill.get("fill_price")) or 0.0, 6),
    )


def classify_path_divergence(
    *,
    coin: str,
    trade_number: int,
    l0_row: dict[str, Any],
    l1_row: dict[str, Any],
    l0_attempts: list[dict[str, Any]],
    l1_attempts: list[dict[str, Any]],
    scale_factor: float = 10.0,
) -> dict[str, Any]:
    l0_fills = list(l0_row.get("fill_log") or [])
    l1_fills = list(l1_row.get("fill_log") or [])
    same_start = int(l0_row.get("start_index") == l1_row.get("start_index"))

    divergence_candle = None
    divergence_cause = ""
    classification = "pure_linear_scaling"

    min_len = min(len(l0_fills), len(l1_fills))
    for i in range(min_len):
        f0, f1 = l0_fills[i], l1_fills[i]
        if f0.get("candle_index") != f1.get("candle_index"):
            divergence_candle = f1.get("candle_index")
            divergence_cause = "different_fill_candle_index"
            classification = "continuous_sequence_divergence"
            break
        if str(f0.get("purpose") or "") != str(f1.get("purpose") or ""):
            divergence_candle = f1.get("candle_index")
            divergence_cause = "different_fill_purpose"
            classification = "exit_path_divergence"
            break
        q0 = safe_float(f0.get("qty")) or 0.0
        q1 = safe_float(f1.get("qty")) or 0.0
        if abs(q1 - q0 * scale_factor) > max(1e-6, q0 * 0.02):
            divergence_candle = f1.get("candle_index")
            divergence_cause = "qty_not_linear_scale"
            classification = "qty_rounding_divergence"
            break
        p0 = safe_float(f0.get("fill_price")) or 0.0
        p1 = safe_float(f1.get("fill_price")) or 0.0
        if abs(p0 - p1) > 1e-9:
            divergence_candle = f1.get("candle_index")
            divergence_cause = "different_fill_price"
            classification = "stage_fill_divergence"
            break

    if divergence_candle is None and len(l0_fills) != len(l1_fills):
        divergence_candle = (l1_fills[min_len] if len(l1_fills) > min_len else {}).get("candle_index")
        divergence_cause = "different_fill_count"
        classification = "continuous_sequence_divergence"

    l0_has_stage = any(a.get("accepted") for a in l0_attempts)
    l1_has_stage = any(a.get("accepted") for a in l1_attempts)
    if l1_has_stage and not l0_has_stage and classification == "pure_linear_scaling":
        classification = "min_notional_path_divergence"
        divergence_cause = divergence_cause or "stage_active_only_in_L1"

    l0_mtm = safe_float(l0_row.get("mtm_pnl"))
    l1_mtm = safe_float(l1_row.get("mtm_pnl"))
    normalized_l0 = l0_mtm / L0_LONG_NOTIONAL * 100.0 if l0_mtm is not None else None
    normalized_l1 = l1_mtm / L1_LONG_NOTIONAL * 100.0 if l1_mtm is not None else None

    return {
        "coin": coin,
        "trade_number": trade_number,
        "identical_start": same_start,
        "first_divergence_candle": divergence_candle,
        "divergence_cause": divergence_cause,
        "classification": classification,
        "stage_active_L0": int(l0_has_stage),
        "stage_active_L1": int(l1_has_stage),
        "L0_mtm_usdt": l0_mtm,
        "L1_mtm_usdt": l1_mtm,
        "L0_normalized_per_100": normalized_l0,
        "L1_normalized_per_100": normalized_l1,
        "L0_status": l0_row.get("status"),
        "L1_status": l1_row.get("status"),
        "L0_max_cycle": l0_row.get("max_cycle"),
        "L1_max_cycle": l1_row.get("max_cycle"),
        "L0_duration": l0_row.get("duration_candles"),
        "L1_duration": l1_row.get("duration_candles"),
    }


def build_blocker_comparison(
    *,
    coin: str,
    l0_rows: list[dict[str, Any]],
    l1_rows: list[dict[str, Any]],
    l0_attempts: list[dict[str, Any]],
    l1_attempts: list[dict[str, Any]],
) -> dict[str, Any]:
    l0_blocker = next((r for r in l0_rows if r.get("is_blocker")), None)
    l1_blocker = next((r for r in l1_rows if r.get("is_blocker")), None)
    if not l0_blocker and not l1_blocker:
        return {}

    l0_closed_before = sum(safe_float(r.get("closed_pnl_usdt")) for r in l0_rows if not r.get("is_blocker"))
    l1_closed_before = sum(safe_float(r.get("closed_pnl_usdt")) for r in l1_rows if not r.get("is_blocker"))
    l0_trade_nums = [int(r.get("trade_number") or 0) for r in l0_rows]
    l1_trade_nums = [int(r.get("trade_number") or 0) for r in l1_rows]

    def _stage_stats(attempts: list[dict[str, Any]], trade_id: int) -> dict[str, Any]:
        rel = [a for a in attempts if int(a.get("trade_id") or 0) == trade_id]
        return {
            "stage_tp_attempts": len(rel),
            "stage_tp_fills": sum(int(a.get("stage_fill_count") or 0) for a in rel if a.get("accepted")),
            "realized_stage_pnl": sum(safe_float(a.get("realized_stage_pnl")) for a in rel),
        }

    l0_id = int((l0_blocker or {}).get("trade_number") or 0)
    l1_id = int((l1_blocker or {}).get("trade_number") or 0)
    l0s = _stage_stats(l0_attempts, l0_id)
    l1s = _stage_stats(l1_attempts, l1_id)

    return {
        "coin": coin,
        "L0_blocker_trade_id": l0_id or None,
        "L1_blocker_trade_id": l1_id or None,
        "same_trade_sequence": int(l0_trade_nums == l1_trade_nums),
        "blocker_start_timestamp_L0": (l0_blocker or {}).get("start_timestamp"),
        "blocker_start_timestamp_L1": (l1_blocker or {}).get("start_timestamp"),
        "highest_cycle_L0": (l0_blocker or {}).get("max_cycle"),
        "highest_cycle_L1": (l1_blocker or {}).get("max_cycle"),
        "gross_notional_L0": (l0_blocker or {}).get("max_total_notional"),
        "gross_notional_L1": (l1_blocker or {}).get("max_total_notional"),
        "net_exposure_L0": (l0_blocker or {}).get("max_abs_net_exposure"),
        "net_exposure_L1": (l1_blocker or {}).get("max_abs_net_exposure"),
        "closed_pnl_before_blocker_L0": l0_closed_before,
        "closed_pnl_before_blocker_L1": l1_closed_before,
        "final_open_mtm_L0": (l0_blocker or {}).get("final_open_mtm_usdt"),
        "final_open_mtm_L1": (l1_blocker or {}).get("final_open_mtm_usdt"),
        "total_coin_result_L0": sum(safe_float(r.get("mtm_pnl")) for r in l0_rows),
        "total_coin_result_L1": sum(safe_float(r.get("mtm_pnl")) for r in l1_rows),
        "stage_tp_attempts_L0": l0s["stage_tp_attempts"],
        "stage_tp_attempts_L1": l1s["stage_tp_attempts"],
        "stage_tp_fills_L0": l0s["stage_tp_fills"],
        "stage_tp_fills_L1": l1s["stage_tp_fills"],
        "realized_stage_pnl_L0": l0s["realized_stage_pnl"],
        "realized_stage_pnl_L1": l1s["realized_stage_pnl"],
        "exit_rebuild_count_L0": (l0_blocker or {}).get("exit_rebuild_count"),
        "exit_rebuild_count_L1": (l1_blocker or {}).get("exit_rebuild_count"),
        "exit_increase_count_L0": (l0_blocker or {}).get("exit_increase_count"),
        "exit_increase_count_L1": (l1_blocker or {}).get("exit_increase_count"),
        "duration_candles_L0": (l0_blocker or {}).get("duration_candles"),
        "duration_candles_L1": (l1_blocker or {}).get("duration_candles"),
    }


def capital_normalized_summary(
    l0: dict[str, Any],
    l1: dict[str, Any],
) -> list[dict[str, Any]]:
    def _norm(row: dict[str, Any], long_n: float, short_n: float) -> dict[str, Any]:
        closed = safe_float(row.get("closed_pnl_usdt"))
        open_m = safe_float(row.get("final_open_mtm_usdt"))
        total = safe_float(row.get("total_series_mtm_usdt"))
        gross = long_n + short_n
        return {
            "variant": row.get("variant"),
            "initial_long_notional_usdt": long_n,
            "initial_short_notional_usdt": short_n,
            "raw_closed_pnl_usdt": closed,
            "raw_open_mtm_usdt": open_m,
            "raw_total_series_mtm_usdt": total,
            "normalized_closed_pnl_per_100_long": closed / long_n * 100.0 if long_n else None,
            "normalized_open_mtm_per_100_long": open_m / long_n * 100.0 if long_n else None,
            "normalized_total_result_per_100_long": total / long_n * 100.0 if long_n else None,
            "gross_normalized_result_per_100_gross": total / gross * 100.0 if gross else None,
        }

    return [
        _norm(l0, L0_LONG_NOTIONAL, L0_SHORT_NOTIONAL),
        _norm(l1, L1_LONG_NOTIONAL, L1_SHORT_NOTIONAL),
    ]


def select_case_study_trades(
    *,
    l0_rows: list[dict[str, Any]],
    l1_rows: list[dict[str, Any]],
    l0_attempts: list[dict[str, Any]],
    l1_attempts: list[dict[str, Any]],
    path_rows: list[dict[str, Any]],
    required_coins: tuple[str, ...] = ("APTUSDT", "UNIUSDT", "BTCUSDT"),
) -> dict[str, dict[str, Any]]:
    picks: dict[str, dict[str, Any]] = {}

    # L0 rejects / L1 accepts
    for a0 in l0_attempts:
        if not a0.get("rejected"):
            continue
        key = (a0["coin"], a0["trade_id"], a0["cycle"])
        a1_match = next(
            (
                a
                for a in l1_attempts
                if (a["coin"], a["trade_id"], a["cycle"]) == key and a.get("accepted")
            ),
            None,
        )
        if a1_match:
            picks["l0_reject_l1_accept"] = {"L0": a0, "L1": a1_match}
            break

    l0_by_key = {(r["coin"], int(r["trade_number"])): r for r in l0_rows}
    l1_by_key = {(r["coin"], int(r["trade_number"])): r for r in l1_rows}

    scored: list[tuple[float, str, dict[str, Any]]] = []
    for pr in path_rows:
        coin = pr["coin"]
        tn = int(pr["trade_number"])
        l0 = l0_by_key.get((coin, tn))
        l1 = l1_by_key.get((coin, tn))
        if not l0 or not l1:
            continue
        n0 = safe_float(pr.get("L0_normalized_per_100"))
        n1 = safe_float(pr.get("L1_normalized_per_100"))
        if n0 is None or n1 is None:
            continue
        delta = n1 - n0
        scored.append((delta, f"{coin} T{tn}", {"path": pr, "L0": l0, "L1": l1}))

    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        picks["better_than_linear"] = scored[0][2]
        picks["worse_than_linear"] = scored[-1][2]

    for coin in required_coins:
        l0b = next((r for r in l0_rows if r["coin"] == coin and r.get("is_blocker")), None)
        l1b = next((r for r in l1_rows if r["coin"] == coin and r.get("is_blocker")), None)
        if l0b or l1b:
            picks[f"blocker_{coin}"] = {"L0": l0b, "L1": l1b, "coin": coin}

    return picks


def freeze_guard_inactive(result: BacktestResult) -> bool:
    excerpt = dict(result.final_strategy_state_excerpt or {})
    variant = excerpt.get("inventory_mtm_freeze_variant")
    # A0 / unset is the research no-op default even when no freeze config is passed.
    if variant not in (None, "A0"):
        return False
    freeze_state = excerpt.get("inventory_mtm_freeze_state") or {}
    if not freeze_state:
        return True
    if freeze_state.get("cycle_freeze_enabled"):
        return False
    safe = freeze_state.get("safe_boundary") or {}
    if safe.get("freeze_state") in {"FREEZE_PENDING", "FREEZE_ACTIVE"}:
        return False
    return True

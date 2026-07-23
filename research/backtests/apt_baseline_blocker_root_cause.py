"""Root-cause analysis helpers for baseline blocker trades (research-only)."""

from __future__ import annotations

import json
import re
import statistics
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.backtests.backtest_report import BacktestResult
from research.backtests.continuous_reentry_backtest import run_continuous_reentry_backtests
from research.backtests.historical_backtest import normalize_candles, run_historical_backtest
from research.backtests.inventory_mtm_freeze import inventory_mtm_usdt, required_recovery_move_pct, safe_float
from research.backtests.long_add_multistart_metrics import (
    analyze_trade,
    build_cycle_rows,
    cycle_leg_map,
    exit_rebuild_stats,
    normalize_trade_status,
)
from research.backtests.long_baseline_notional_stage_tp import build_baseline_call_kwargs
from research.backtests.recovery_wait_activation import TradeFillReplayRow, replay_state_at_absolute_index
from research.backtests.run_inventory_mtm_neg1_policy_audit import (
    BASELINE_DIR,
    LONG_FILL_DISTANCE_PCT,
    TARGET_PROFIT_USDT,
    TP_PROFIT_TARGET_PCT,
)

CYCLE_PURPOSE_RE = re.compile(r"^CYCLE_(\d+)_(LONG_ADD|SHORT_REDUCE)$")

# APTUSDT trade 3 parity (protected baseline / L0 stage audit).
APT_TRADE3_COIN = "APTUSDT"
APT_TRADE3_ID = 3
APT_TRADE3_START_INDEX = 570
APT_TRADE3_MAX_CYCLE = 8
APT_TRADE3_MTM = -9.45168353402973
APT_TRADE3_REALIZED = 4.414452087090382
APT_TRADE3_MTM_TOLERANCE = 0.02
APT_TRADE3_CYCLE_TOLERANCE = 0


PROTECTED_OUTPUT_DIRS = (
    BASELINE_DIR,
    Path(__file__).resolve().parents[2]
    / "research/backtests/results/safe_cycle_boundary_freeze_audit_20260720",
    Path(__file__).resolve().parents[2]
    / "research/backtests/results/long_baseline_1000_500_stage_tp_audit_20260721",
)


def _ts(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _purpose(fill: dict[str, Any]) -> str:
    return str(fill.get("purpose") or fill.get("order_purpose") or "")


def _candle_high(candle: Any) -> float:
    if isinstance(candle, dict):
        return float(candle.get("high") or candle.get("close") or 0.0)
    return float(getattr(candle, "high", None) or getattr(candle, "close", 0.0) or 0.0)


def _candle_close(candle: Any) -> float:
    if isinstance(candle, dict):
        return float(candle.get("close") or 0.0)
    return float(getattr(candle, "close", 0.0) or 0.0)


def assert_output_dir_safe(output_dir: Path) -> None:
    resolved = output_dir.resolve()
    for protected in PROTECTED_OUTPUT_DIRS:
        if resolved == protected.resolve():
            raise RuntimeError(f"refusing protected output dir: {protected}")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"refusing to overwrite non-empty output dir: {output_dir}")


def run_baseline_single_trade(
    *,
    coin: str,
    candles: list[Any],
    start_index: int,
) -> BacktestResult:
    """Pure baseline — no freeze, recovery, or exit-rebuild policy."""
    window = candles[start_index:]
    result = run_historical_backtest(
        coin.upper(),
        "long",
        window,
        config_source="live",
        fill_model="conservative",
        tp_profit_target_pct=TP_PROFIT_TARGET_PCT,
        long_fill_distance_pct=LONG_FILL_DISTANCE_PCT,
        target_profit_usdt=TARGET_PROFIT_USDT,
        absolute_trade_start_index=start_index,
    )
    result.start_index = start_index
    result.trade_number = result.trade_number or 1
    return result


def select_trade_from_continuous(
    *,
    coin: str,
    candles: list[Any],
    trade_id: int,
) -> tuple[BacktestResult, dict[str, Any]]:
    """Run continuous baseline chain and return the requested trade_number."""
    payload = run_continuous_reentry_backtests(
        **build_baseline_call_kwargs(symbol=coin, candles=candles, base_notional_usdt=100.0)
    )
    results: list[BacktestResult] = list(payload.get("results") or [])
    for result in results:
        if int(result.trade_number or 0) == int(trade_id):
            meta = {
                "trade_number": int(result.trade_number or 0),
                "start_index": int(result.start_index or 0),
                "total_trades_in_chain": len(results),
            }
            return result, meta
    raise ValueError(f"{coin}: trade_id {trade_id} not found (chain has {len(results)} trades)")


def load_trade_start_index_from_baseline(coin: str, trade_id: int) -> int | None:
    """Best-effort lookup from protected baseline continuous_trade_details.csv."""
    path = BASELINE_DIR / "continuous_trade_details.csv"
    if not path.exists():
        return None
    import csv

    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if str(row.get("coin") or "").upper() != coin.upper():
                continue
            if int(safe_float(row.get("trade_number"), -1)) != int(trade_id):
                continue
            idx = row.get("start_index")
            return int(idx) if idx not in (None, "") else None
    return None


def build_fill_replay_rows(
    result: BacktestResult,
    *,
    start_index: int,
) -> list[TradeFillReplayRow]:
    rows: list[TradeFillReplayRow] = []
    cum = 0.0
    for fill in result.fill_log or []:
        closed = safe_float(fill.get("closed_pnl") or fill.get("confirmed_closed_pnl"))
        cum += closed
        local = fill.get("candle_index")
        abs_idx = (start_index + int(local)) if local is not None else start_index
        purpose = _purpose(fill)
        cycle_match = CYCLE_PURPOSE_RE.match(purpose)
        cycle_index = int(cycle_match.group(1)) if cycle_match else None
        long_qty = safe_float(fill.get("long_qty_after"))
        short_qty = safe_float(fill.get("short_qty_after"))
        rows.append(
            TradeFillReplayRow(
                absolute_candle_index=abs_idx,
                timestamp=_ts(fill.get("timestamp")),
                purpose=purpose,
                fill_price=safe_float(fill.get("fill_price")) or None,
                long_qty_after=long_qty,
                short_qty_after=short_qty,
                long_avg_after=safe_float(fill.get("long_avg_after")),
                short_avg_after=safe_float(fill.get("short_avg_after")),
                cumulative_realized_pnl_net=cum,
                cycle_index=cycle_index,
                flat_after_fill=long_qty <= 1e-9 and short_qty <= 1e-9,
            )
        )
    return rows


def _active_exit_at_local_candle(
    order_log: list[dict[str, Any]],
    *,
    local_candle: int,
) -> float | None:
    active: float | None = None
    for order in sorted(order_log, key=lambda o: (o.get("candle_index") or -1, str(o.get("timestamp") or ""))):
        ci = order.get("candle_index")
        if ci is None:
            continue
        try:
            if int(ci) > local_candle:
                break
        except (TypeError, ValueError):
            continue
        if str(order.get("purpose") or "") != "LONG_TP_EXIT":
            continue
        event = str(order.get("event_type") or "").lower()
        price = safe_float(order.get("trigger_price") or order.get("price"))
        if event == "submitted" and price > 0:
            active = price
        elif event in {"cancelled", "canceled"}:
            active = None
    return active


def build_cycle_snapshots(
    *,
    result: BacktestResult,
    candles: list[Any],
    start_index: int,
) -> list[dict[str, Any]]:
    """One row per completed cycle boundary (after SHORT_REDUCE) + final open state."""
    fills = list(result.fill_log or [])
    order_log = list(result.order_log or [])
    legs = cycle_leg_map(fills)
    snapshots: list[dict[str, Any]] = []
    cum_realized = 0.0

    for cycle in sorted(legs.keys()):
        entry = legs[cycle]
        short_reduce = entry.get("short_reduce")
        if not short_reduce:
            continue
        local = int(short_reduce["candle_index"])
        abs_candle = start_index + local
        mark = _candle_close(candles[abs_candle]) if abs_candle < len(candles) else safe_float(short_reduce.get("fill_price"))
        long_qty = safe_float(short_reduce.get("long_qty_after"))
        short_qty = safe_float(short_reduce.get("short_qty_after"))
        long_avg = safe_float(short_reduce.get("long_avg_after"))
        short_avg = safe_float(short_reduce.get("short_avg_after"))
        for f in fills:
            if int(f.get("candle_index") or -1) <= local:
                cum_realized += safe_float(f.get("closed_pnl") or f.get("confirmed_closed_pnl"))
        active_exit = _active_exit_at_local_candle(order_log, local_candle=local)
        mtm = inventory_mtm_usdt(
            realized=cum_realized,
            long_qty=long_qty,
            long_avg=long_avg,
            short_qty=short_qty,
            short_avg=short_avg,
            mark=mark,
        )
        exit_dist = required_recovery_move_pct(mark=mark, active_exit=active_exit, primary_side="long")
        long_add = entry.get("long_add") or {}
        snapshots.append(
            {
                "cycle": cycle,
                "phase": "after_short_reduce",
                "local_candle": local,
                "absolute_candle": abs_candle,
                "timestamp": short_reduce.get("timestamp"),
                "mark": mark,
                "long_qty": long_qty,
                "short_qty": short_qty,
                "long_avg": long_avg,
                "short_avg": short_avg,
                "net_exposure_qty": long_qty - short_qty,
                "gross_notional_usdt": long_qty * long_avg + short_qty * short_avg,
                "net_exposure_usdt": (long_qty - short_qty) * mark,
                "active_exit": active_exit,
                "exit_distance_pct": exit_dist,
                "inventory_mtm_usdt": mtm,
                "cum_realized_pnl": cum_realized,
                "cycle_net_pnl": safe_float(long_add.get("closed_pnl")) + safe_float(short_reduce.get("closed_pnl")),
                "long_add_candle": (long_add or {}).get("candle_index"),
                "short_reduce_candle": local,
            }
        )

    if snapshots:
        final = dict(snapshots[-1])
        final["phase"] = "final_open_state"
        final["inventory_mtm_usdt"] = safe_float(result.overall_pnl, safe_float(result.realized_pnl) + safe_float(result.unrealized_pnl))
        final["cum_realized_pnl"] = safe_float(result.realized_pnl)
        final["active_exit"] = safe_float(
            next(
                (
                    safe_float(o.get("trigger_price") or o.get("price"))
                    for o in (result.final_active_orders or [])
                    if str(o.get("purpose") or "") == "LONG_TP_EXIT"
                ),
                None,
            )
        )
        mark_end = safe_float(result.final_price)
        final["mark"] = mark_end
        final["exit_distance_pct"] = required_recovery_move_pct(
            mark=mark_end, active_exit=final.get("active_exit"), primary_side="long"
        )
        snapshots.append(final)
    return snapshots


def build_exit_reachability_by_cycle(
    *,
    result: BacktestResult,
    candles: list[Any],
    start_index: int,
    snapshots: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Per cycle: was active exit ever reachable again after cycle completed?"""
    window = candles[start_index:]
    rebuild = exit_rebuild_stats(result, window_candles=window)
    rebuilds = list(rebuild.get("rebuilds") or [])
    rows: list[dict[str, Any]] = []

    for snap in snapshots:
        if snap.get("phase") != "after_short_reduce":
            continue
        cycle = int(snap["cycle"])
        local = int(snap["local_candle"])
        active_exit = safe_float(snap.get("active_exit"))
        mark = safe_float(snap.get("mark"))
        future = window[local + 1 :]
        max_high = max((_candle_high(c) for c in future), default=mark) if future else mark
        reachable_now = bool(active_exit and mark <= active_exit + 1e-9)
        reachable_later = bool(active_exit and max_high >= active_exit - 1e-9)
        harmful_after = [
            r
            for r in rebuilds
            if r.get("candle_index") is not None and int(r["candle_index"]) >= local and r.get("replaced_reachable_with_unreachable")
        ]
        rows.append(
            {
                "cycle": cycle,
                "local_candle_after_cycle": local,
                "active_exit_after_cycle": active_exit,
                "mark_at_cycle_end": mark,
                "exit_distance_pct": snap.get("exit_distance_pct"),
                "reachable_at_cycle_end": int(reachable_now),
                "max_high_after_cycle": max_high,
                "reachable_later_in_sample": int(reachable_later),
                "harmful_exit_rebuilds_after_cycle": len(harmful_after),
                "first_harmful_rebuild_candle": (
                    int(harmful_after[0]["candle_index"]) if harmful_after else None
                ),
            }
        )
    return rows


def build_exposure_growth_by_cycle(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    prev_gross = prev_net = prev_exit = None
    for snap in snapshots:
        if snap.get("phase") != "after_short_reduce":
            continue
        gross = safe_float(snap.get("gross_notional_usdt"))
        net = abs(safe_float(snap.get("net_exposure_usdt")))
        exit_p = safe_float(snap.get("active_exit"))
        rows.append(
            {
                "cycle": snap["cycle"],
                "gross_notional_usdt": gross,
                "net_exposure_usdt": safe_float(snap.get("net_exposure_usdt")),
                "abs_net_exposure_usdt": net,
                "active_exit": exit_p,
                "delta_gross_vs_prev": (gross - prev_gross) if prev_gross is not None else None,
                "delta_abs_net_vs_prev": (net - abs(prev_net or 0.0)) if prev_net is not None else None,
                "delta_exit_vs_prev": (exit_p - prev_exit) if prev_exit is not None and exit_p else None,
                "inventory_mtm_usdt": snap.get("inventory_mtm_usdt"),
                "exit_distance_pct": snap.get("exit_distance_pct"),
            }
        )
        prev_gross, prev_net, prev_exit = gross, safe_float(snap.get("net_exposure_usdt")), exit_p
    return rows


def build_event_timeline(
    *,
    result: BacktestResult,
    start_index: int,
) -> list[dict[str, Any]]:
    events: list[tuple[int, str, dict[str, Any]]] = []
    for fill in result.fill_log or []:
        local = int(fill.get("candle_index") or 0)
        events.append(
            (
                local,
                "fill",
                {
                    "event_kind": "fill",
                    "local_candle": local,
                    "absolute_candle": start_index + local,
                    "timestamp": fill.get("timestamp"),
                    "purpose": _purpose(fill),
                    "side": fill.get("side"),
                    "qty": fill.get("qty"),
                    "fill_price": fill.get("fill_price"),
                    "closed_pnl": safe_float(fill.get("closed_pnl") or fill.get("confirmed_closed_pnl")),
                    "long_qty_after": fill.get("long_qty_after"),
                    "short_qty_after": fill.get("short_qty_after"),
                },
            )
        )
    for order in result.order_log or []:
        purpose = str(order.get("purpose") or "")
        if purpose != "LONG_TP_EXIT":
            continue
        event_type = str(order.get("event_type") or "").lower()
        if event_type not in {"submitted", "cancelled", "canceled"}:
            continue
        local = int(order.get("candle_index") or 0)
        events.append(
            (
                local,
                f"order_{event_type}",
                {
                    "event_kind": f"order_{event_type}",
                    "local_candle": local,
                    "absolute_candle": start_index + local,
                    "timestamp": order.get("timestamp"),
                    "purpose": purpose,
                    "trigger_price": order.get("trigger_price") or order.get("price"),
                },
            )
        )
    events.sort(key=lambda item: (item[0], item[1]))
    return [payload for _, _, payload in events]


def pnl_reconciliation_rows(result: BacktestResult) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cum = fee_cum = 0.0
    for fill in result.fill_log or []:
        closed = safe_float(fill.get("closed_pnl") or fill.get("confirmed_closed_pnl"))
        entry_fee = safe_float(fill.get("entry_fee"))
        exit_fee = safe_float(fill.get("exit_fee"))
        fees = entry_fee + exit_fee
        cum += closed
        fee_cum += fees
        gross = safe_float(fill.get("gross_realized_pnl_event") or fill.get("gross_pnl"))
        rows.append(
            {
                "purpose": _purpose(fill),
                "local_candle": fill.get("candle_index"),
                "closed_pnl": closed,
                "gross_pnl": gross if gross else None,
                "entry_fee": entry_fee or None,
                "exit_fee": exit_fee or None,
                "fees": fees or None,
                "cum_closed_pnl": cum,
                "cum_fees": fee_cum,
                "identity_gross_minus_fees": (gross - fees) if gross else None,
                "identity_vs_closed": ((gross - fees) - closed) if gross else None,
            }
        )
    rows.append(
        {
            "purpose": "TOTAL",
            "closed_pnl": cum,
            "cum_closed_pnl": cum,
            "cum_fees": fee_cum,
            "overall_pnl_reported": safe_float(result.overall_pnl),
            "realized_reported": safe_float(result.realized_pnl),
            "unrealized_reported": safe_float(result.unrealized_pnl),
            "recon_ok": abs(cum - safe_float(result.realized_pnl)) < 1e-6,
        }
    )
    return rows


def analyze_root_cause_markers(
    *,
    snapshots: list[dict[str, Any]],
    reachability: list[dict[str, Any]],
    exposure_rows: list[dict[str, Any]],
    result: BacktestResult,
    candles: list[Any],
    start_index: int,
) -> dict[str, Any]:
    """Detect last healthy cycle, escalation start, point of no return."""
    completed = [s for s in snapshots if s.get("phase") == "after_short_reduce"]
    if not completed:
        return {}

    # Last healthy: last cycle with mtm >= -1.0 OR exit_distance <= 3% (whichever latest)
    healthy_candidates = [
        s
        for s in completed
        if safe_float(s.get("inventory_mtm_usdt")) >= -1.0
        or (safe_float(s.get("exit_distance_pct")) or 999.0) <= 3.0
    ]
    last_healthy = healthy_candidates[-1] if healthy_candidates else completed[0]

    # Escalation: first cycle after last_healthy where gross notional grows AND exit increases
    last_healthy_cycle = int(last_healthy["cycle"])
    escalation = None
    for row in exposure_rows:
        if int(row["cycle"]) <= last_healthy_cycle:
            continue
        if (safe_float(row.get("delta_gross_vs_prev")) or 0.0) > 0 and (safe_float(row.get("delta_exit_vs_prev")) or 0.0) > 0:
            escalation = row
            break
    if escalation is None and len(exposure_rows) > last_healthy_cycle:
        escalation = next((r for r in exposure_rows if int(r["cycle"]) == last_healthy_cycle + 1), None)

    # Point of no return: first harmful exit rebuild after last healthy, or first cycle where
    # reachable_later_in_sample flips to 0 while exit_distance > 5%
    por = None
    por_reason = ""
    for row in reachability:
        if int(row["cycle"]) < last_healthy_cycle:
            continue
        if int(row.get("harmful_exit_rebuilds_after_cycle") or 0) > 0:
            por = row
            por_reason = "harmful_exit_rebuild_replaced_reachable_exit"
            break
        if not row.get("reachable_later_in_sample") and safe_float(row.get("exit_distance_pct")) > 5.0:
            por = row
            por_reason = "exit_unreachable_and_distance_gt_5pct"
            break

    # Old exit reachable later: count from exit_rebuild_stats
    window = candles[start_index:]
    rebuild = exit_rebuild_stats(result, window_candles=window)
    old_exit_reachable_count = int(rebuild.get("old_exit_later_reachable_count") or 0)

    return {
        "last_healthy_cycle": int(last_healthy["cycle"]),
        "last_healthy_local_candle": int(last_healthy["local_candle"]),
        "last_healthy_timestamp": last_healthy.get("timestamp"),
        "last_healthy_mtm_usdt": safe_float(last_healthy.get("inventory_mtm_usdt")),
        "last_healthy_exit_distance_pct": safe_float(last_healthy.get("exit_distance_pct")),
        "last_healthy_active_exit": safe_float(last_healthy.get("active_exit")),
        "escalation_begin_cycle": int(escalation["cycle"]) if escalation else None,
        "escalation_begin_gross_delta": safe_float((escalation or {}).get("delta_gross_vs_prev")),
        "escalation_begin_exit_delta": safe_float((escalation or {}).get("delta_exit_vs_prev")),
        "point_of_no_return_cycle": int(por["cycle"]) if por else None,
        "point_of_no_return_reason": por_reason if por else None,
        "point_of_no_return_local_candle": int(por["local_candle_after_cycle"]) if por else None,
        "old_exit_later_reachable_count": old_exit_reachable_count,
        "max_cycle_reached": max(int(s["cycle"]) for s in completed),
        "final_mtm_usdt": safe_float(result.overall_pnl),
        "final_exit_distance_pct": safe_float(completed[-1].get("exit_distance_pct")) if completed else None,
    }


def build_recovery_start_state(
    *,
    coin: str,
    trade_id: int,
    start_index: int,
    snapshots: list[dict[str, Any]],
    markers: dict[str, Any],
    replay_rows: list[TradeFillReplayRow],
) -> dict[str, Any]:
    """Exact safe start state at end of last healthy cycle for future recovery tests."""
    target_cycle = int(markers.get("last_healthy_cycle") or 0)
    snap = next((s for s in snapshots if s.get("phase") == "after_short_reduce" and int(s["cycle"]) == target_cycle), None)
    if not snap:
        snap = next((s for s in snapshots if s.get("phase") == "after_short_reduce"), {})

    abs_candle = int(snap.get("absolute_candle") or start_index)
    replay = replay_state_at_absolute_index(replay_rows, abs_candle)

    return {
        "coin": coin.upper(),
        "trade_id": trade_id,
        "baseline_trade_start_index": start_index,
        "recovery_anchor_cycle": target_cycle,
        "recovery_anchor_local_candle": int(snap.get("local_candle") or 0),
        "recovery_anchor_absolute_candle": abs_candle,
        "recovery_anchor_timestamp": snap.get("timestamp"),
        "mark_at_anchor": safe_float(snap.get("mark")),
        "long_qty": safe_float(snap.get("long_qty")),
        "short_qty": safe_float(snap.get("short_qty")),
        "long_avg": safe_float(snap.get("long_avg")),
        "short_avg": safe_float(snap.get("short_avg")),
        "net_exposure_qty": safe_float(snap.get("net_exposure_qty")),
        "gross_notional_usdt": safe_float(snap.get("gross_notional_usdt")),
        "active_exit_at_anchor": safe_float(snap.get("active_exit")),
        "exit_distance_pct_at_anchor": safe_float(snap.get("exit_distance_pct")),
        "inventory_mtm_usdt_at_anchor": safe_float(snap.get("inventory_mtm_usdt")),
        "cum_realized_pnl_at_anchor": safe_float(snap.get("cum_realized_pnl")),
        "replay_state": asdict(replay) if replay else None,
        "note": (
            "Inject this state at recovery_anchor_absolute_candle for recovery counterfactuals. "
            "Uses baseline fill replay — no freeze/recovery policy in source run."
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def check_baseline_parity(
    *,
    coin: str,
    trade_id: int,
    result: BacktestResult,
    analysis: dict[str, Any],
) -> dict[str, Any]:
    if coin.upper() != APT_TRADE3_COIN or int(trade_id) != APT_TRADE3_ID:
        return {"skipped": True, "reason": "parity targets defined for APTUSDT trade 3 only"}

    mtm = safe_float(analysis.get("mtm_pnl") or result.overall_pnl)
    max_cycle = int(analysis.get("max_cycle") or 0)
    checks = {
        "start_index": (
            int(result.start_index or 0),
            APT_TRADE3_START_INDEX,
            int(result.start_index or 0) == APT_TRADE3_START_INDEX,
        ),
        "max_cycle": (
            max_cycle,
            APT_TRADE3_MAX_CYCLE,
            max_cycle == APT_TRADE3_MAX_CYCLE,
        ),
        "mtm_pnl": (
            mtm,
            APT_TRADE3_MTM,
            abs(mtm - APT_TRADE3_MTM) <= APT_TRADE3_MTM_TOLERANCE,
        ),
        "status_open": (
            normalize_trade_status(result),
            "open",
            normalize_trade_status(result) != "closed",
        ),
        "invalid_partial": (
            analysis.get("undercoverage"),
            0,
            int(analysis.get("undercoverage") or 0) == 0,
        ),
    }
    return {"ok": all(c[2] for c in checks.values()), "checks": checks}


def selected_trade_payload(
    *,
    coin: str,
    trade_id: int,
    result: BacktestResult,
    meta: dict[str, Any],
    analysis: dict[str, Any],
    markers: dict[str, Any],
    parity: dict[str, Any],
) -> dict[str, Any]:
    return {
        "coin": coin.upper(),
        "trade_id": int(trade_id),
        "start_index": int(result.start_index or 0),
        "end_index_absolute": int(result.start_index or 0) + int(result.candles_processed or 0),
        "start_timestamp": _ts(result.start_time),
        "end_timestamp": _ts(result.end_time),
        "status": normalize_trade_status(result),
        "exit_reason": result.exit_reason,
        "duration_candles": int(result.candles_processed or 0),
        "realized_pnl_usdt": safe_float(result.realized_pnl),
        "unrealized_pnl_usdt": safe_float(result.unrealized_pnl),
        "mtm_pnl_usdt": safe_float(result.overall_pnl),
        "max_cycle": analysis.get("max_cycle"),
        "exit_rebuild_count": analysis.get("exit_rebuild_count"),
        "exit_increase_count": analysis.get("exit_increase_count"),
        "old_exit_later_reachable_count": analysis.get("old_exit_later_reachable_count"),
        "continuous_meta": meta,
        "root_cause_markers": markers,
        "baseline_parity": parity,
        "policy_disabled": [
            "inventory_mtm_freeze",
            "safe_cycle_boundary",
            "recovery_reentry",
            "exit_rebuild_policy",
        ],
    }


def write_code_path_map(path: Path) -> None:
    path.write_text(
        """# Code path map — baseline blocker root-cause audit

## Trade selection

- `apt_baseline_blocker_root_cause.select_trade_from_continuous`
- Uses `continuous_reentry_backtest.run_continuous_reentry_backtests` with
  `build_baseline_call_kwargs` (live config, 100 USDT long, no policy kwargs).

## Single-trade replay

- `historical_backtest.run_historical_backtest` when `--start-index` is supplied.
- Fill replay: `recovery_wait_activation.replay_state_at_absolute_index`.

## inventory_mtm_usdt

- `inventory_mtm_freeze.inventory_mtm_usdt`
- `realized + long_qty*(mark-long_avg) + short_qty*(short_avg-mark)` at candle close.

## Cycle boundaries

- `long_add_multistart_metrics.cycle_leg_map` / `build_cycle_rows`
- Snapshot taken after each `CYCLE_N_SHORT_REDUCE` fill.

## Exit reachability

- `long_add_multistart_metrics.exit_rebuild_stats` with full trade window candles.
- `replaced_reachable_with_unreachable`: new exit higher than old and future
  `high` never reaches old exit before sample end.

## Exposure growth

- Gross notional = `long_qty*long_avg + short_qty*short_avg` at cycle end.
- Deltas vs previous completed cycle.

## Root-cause markers (heuristic)

1. **Last healthy cycle** — last completed cycle with `inventory_mtm >= -1.0`
   OR `exit_distance_pct <= 3%`.
2. **Escalation begin** — first later cycle with gross notional up AND exit price up.
3. **Point of no return** — first post-healthy cycle with harmful exit rebuild OR
   exit not reachable later while distance > 5%.

## Recovery start state

- State at end of **last healthy cycle** from fill replay + cycle snapshot.
- Safe injection point for future recovery counterfactuals (research-only).

## PnL reconciliation

- Sum of fill `closed_pnl` vs `result.realized_pnl`.
""",
        encoding="utf-8",
    )


def write_report(
    path: Path,
    *,
    selected: dict[str, Any],
    markers: dict[str, Any],
    recovery_state: dict[str, Any],
    parity: dict[str, Any],
) -> None:
    lines = [
        "# Baseline Blocker Root-Cause Audit",
        "",
        f"Generated: `{datetime.now(timezone.utc).isoformat()}`",
        "",
        f"- Coin: **{selected['coin']}** trade **{selected['trade_id']}**",
        f"- Start index: `{selected['start_index']}` ({selected['start_timestamp']})",
        f"- Status: `{selected['status']}` | max cycle: `{selected['max_cycle']}`",
        f"- MTM: `{selected['mtm_pnl_usdt']:.4f}` USDT (realized `{selected['realized_pnl_usdt']:.4f}`, "
        f"unrealized `{selected['unrealized_pnl_usdt']:.4f}`)",
        "",
        "## Baseline parity",
        "",
    ]
    if parity.get("skipped"):
        lines.append(f"_Parity check skipped: {parity.get('reason')}_")
    else:
        lines.append(f"- **{'PASS' if parity.get('ok') else 'FAIL'}**")
        for name, (actual, expected, ok) in parity.get("checks", {}).items():
            lines.append(f"- {name}: `{actual}` vs `{expected}` → {ok}")

    lines.extend(
        [
            "",
            "## Root-cause markers",
            "",
            f"1. **Last healthy cycle:** `{markers.get('last_healthy_cycle')}` "
            f"(local candle `{markers.get('last_healthy_local_candle')}`, "
            f"mtm `{safe_float(markers.get('last_healthy_mtm_usdt')):.4f}`, "
            f"exit distance `{safe_float(markers.get('last_healthy_exit_distance_pct')):.2f}%`)",
            f"2. **Escalation begin:** cycle `{markers.get('escalation_begin_cycle')}` "
            f"(Δgross `{markers.get('escalation_begin_gross_delta')}`, "
            f"Δexit `{markers.get('escalation_begin_exit_delta')}`)",
            f"3. **Point of no return:** cycle `{markers.get('point_of_no_return_cycle')}` — "
            f"{markers.get('point_of_no_return_reason') or 'n/a'}",
            f"4. **Old exit later reachable (harmful rebuilds):** `{markers.get('old_exit_later_reachable_count')}`",
            f"5. **Max cycle reached:** `{markers.get('max_cycle_reached')}`",
            "",
            "## Recovery injection anchor",
            "",
            f"- Use `selected_recovery_start_state.json` at absolute candle "
            f"`{recovery_state.get('recovery_anchor_absolute_candle')}` "
            f"(after cycle `{recovery_state.get('recovery_anchor_cycle')}`).",
            "",
            "## Artifacts",
            "",
            "- `event_timeline.csv` — fills + LONG_TP_EXIT submit/cancel",
            "- `cycle_snapshots.csv` — inventory after each cycle",
            "- `exit_reachability_by_cycle.csv` — post-cycle exit reachability",
            "- `exposure_growth_by_cycle.csv` — gross/net/exit deltas",
            "- `pnl_reconciliation.csv` — fill-level PnL identity",
            "- `healthy_escalation_no_return.json` — marker summary",
            "",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

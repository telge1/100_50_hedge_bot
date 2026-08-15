"""Metrics helpers for causal LONG_ADD multi-start comparisons (research-only)."""

from __future__ import annotations

import math
import re
import statistics
from collections import defaultdict
from typing import Any, Iterable

from research.backtests.backtest_report import BacktestResult
from research.backtests.pnl_coverage_audit import build_pnl_coverage_audit

CYCLE_LEG_RE = re.compile(r"^CYCLE_(\d+)_(LONG_ADD|SHORT_REDUCE)$")
INITIAL_LONG_PURPOSE = "INITIAL_LONG_ENTRY"
BASELINE_LONG_ADD_PCT = 0.5
FEE_FALLBACK = 0.00055


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (q / 100.0)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    weight = rank - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def variant_dir_name(long_add_pct: float) -> str:
    return f"la_{str(long_add_pct).replace('.', '_')}"


def normalize_trade_status(result: BacktestResult) -> str:
    if result.error:
        return "error"
    status = str(result.final_status or "").lower()
    if status == "closed":
        return "closed"
    if status in {"error", "failed"}:
        return "error"
    return "open"


def has_initial_entry_fill(result: BacktestResult) -> bool:
    for fill in result.fill_log or []:
        purpose = str(fill.get("purpose") or "")
        if purpose == INITIAL_LONG_PURPOSE or purpose.endswith("INITIAL_LONG_ENTRY"):
            return True
    return bool(result.entry_price is not None and int(result.fills_count or 0) > 0)


def classify_start(
    result: BacktestResult | None,
    *,
    window_candles: int,
    planned: bool = True,
) -> tuple[bool, str]:
    if not planned:
        return False, "not_planned"
    if result is None:
        return False, "missing_result"
    if result.error:
        return False, f"error:{result.error}"
    window_len = int(result.window_candles or 0)
    if window_len < window_candles:
        return False, "incomplete_window"
    if not has_initial_entry_fill(result):
        return False, "no_initial_entry_fill"
    return True, "ok"


def same_candle_long_add_short_reduce(fills: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by: dict[tuple[Any, int], dict[str, list]] = defaultdict(lambda: {"long": [], "short": []})
    for fill in fills:
        purpose = str(fill.get("purpose") or "")
        match = CYCLE_LEG_RE.match(purpose)
        if not match:
            continue
        cycle = int(match.group(1))
        leg = match.group(2)
        key = (fill.get("candle_index"), cycle)
        if leg == "LONG_ADD":
            by[key]["long"].append(fill)
        else:
            by[key]["short"].append(fill)
    cases: list[dict[str, Any]] = []
    for (candle_index, cycle), sides in sorted(by.items(), key=lambda item: (item[0][0] is None, item[0])):
        if sides["long"] and sides["short"]:
            long_fill = sides["long"][0]
            short_fill = sides["short"][0]
            cases.append(
                {
                    "candle_index": candle_index,
                    "cycle": cycle,
                    "timestamp": long_fill.get("timestamp"),
                    "long_purpose": long_fill.get("purpose"),
                    "short_purpose": short_fill.get("purpose"),
                    "long_price": long_fill.get("fill_price"),
                    "short_price": short_fill.get("fill_price"),
                }
            )
    return cases


def _fee_for_fill(fill: dict[str, Any]) -> float:
    qty = abs(safe_float(fill.get("qty")))
    price = safe_float(fill.get("fill_price"))
    rate = safe_float(fill.get("fee_rate"), FEE_FALLBACK)
    return qty * price * rate


def exposure_from_fills(fills: Iterable[dict[str, Any]]) -> dict[str, float]:
    max_long = max_short = 0.0
    max_long_notional = max_short_notional = 0.0
    max_total_notional = max_net_abs = 0.0
    fees = 0.0
    for fill in fills:
        long_qty = safe_float(fill.get("long_qty_after"))
        short_qty = safe_float(fill.get("short_qty_after"))
        long_avg = safe_float(fill.get("long_avg_after"))
        short_avg = safe_float(fill.get("short_avg_after"))
        px = safe_float(fill.get("candle_close") or fill.get("fill_price"))
        long_notional = long_qty * (long_avg if long_avg > 0 else px)
        short_notional = short_qty * (short_avg if short_avg > 0 else px)
        max_long = max(max_long, long_qty)
        max_short = max(max_short, short_qty)
        max_long_notional = max(max_long_notional, long_notional)
        max_short_notional = max(max_short_notional, short_notional)
        max_total_notional = max(max_total_notional, long_notional + short_notional)
        max_net_abs = max(max_net_abs, abs(long_qty - short_qty))
        fees += _fee_for_fill(fill)
    return {
        "max_long_qty": max_long,
        "max_short_qty": max_short,
        "max_long_notional": max_long_notional,
        "max_short_notional": max_short_notional,
        "max_total_notional": max_total_notional,
        "max_abs_net_exposure": max_net_abs,
        "fees": fees,
    }


def cycle_leg_map(fills: Iterable[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    cycles: dict[int, dict[str, Any]] = {}
    for fill in fills:
        purpose = str(fill.get("purpose") or "")
        match = CYCLE_LEG_RE.match(purpose)
        if not match:
            continue
        cycle = int(match.group(1))
        leg = match.group(2)
        entry = cycles.setdefault(cycle, {"cycle": cycle, "long_add": None, "short_reduce": None})
        payload = {
            "purpose": purpose,
            "timestamp": fill.get("timestamp"),
            "candle_index": fill.get("candle_index"),
            "fill_price": fill.get("fill_price"),
            "closed_pnl": safe_float(fill.get("closed_pnl") or fill.get("confirmed_closed_pnl")),
            "qty": safe_float(fill.get("qty")),
            "long_qty_after": safe_float(fill.get("long_qty_after")),
            "short_qty_after": safe_float(fill.get("short_qty_after")),
            "long_avg_after": safe_float(fill.get("long_avg_after")),
            "short_avg_after": safe_float(fill.get("short_avg_after")),
        }
        if leg == "LONG_ADD":
            entry["long_add"] = payload
        else:
            entry["short_reduce"] = payload
    return cycles


def build_cycle_rows(
    *,
    variant: str,
    long_add_pct: float,
    start_index: int,
    fills: Iterable[dict[str, Any]],
    target_profit_usdt: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cycle, legs in sorted(cycle_leg_map(fills).items()):
        long_add = legs.get("long_add") or {}
        short_reduce = legs.get("short_reduce") or {}
        loss_pnl = safe_float(long_add.get("closed_pnl")) if long_add else 0.0
        cover_pnl = safe_float(short_reduce.get("closed_pnl")) if short_reduce else 0.0
        first_leg_loss = abs(loss_pnl) if loss_pnl < 0 else 0.0
        second_leg_gain = cover_pnl if short_reduce else None
        required_net = first_leg_loss + target_profit_usdt if long_add else None
        realized_net = (cover_pnl + loss_pnl) if (long_add and short_reduce) else None
        coverage_margin = None
        if required_net is not None and short_reduce:
            coverage_margin = cover_pnl - required_net
        long_ci = long_add.get("candle_index")
        short_ci = short_reduce.get("candle_index")
        duration = None
        if long_ci is not None and short_ci is not None:
            try:
                duration = int(short_ci) - int(long_ci)
            except (TypeError, ValueError):
                duration = None
        complete = bool(long_add and short_reduce)
        rows.append(
            {
                "variant": variant,
                "long_add_pct": long_add_pct,
                "start_index": start_index,
                "cycle_index": cycle,
                "first_leg_loss": first_leg_loss if long_add else None,
                "second_leg_gain": second_leg_gain,
                "cycle_net": realized_net,
                "coverage_margin": coverage_margin,
                "complete": complete,
                "duration_first_to_second_candles": duration,
                "long_add_price": long_add.get("fill_price"),
                "short_reduce_price": short_reduce.get("fill_price"),
                "net_exposure_before": (
                    safe_float(long_add.get("long_qty_after")) - safe_float(long_add.get("short_qty_after"))
                    if long_add
                    else None
                ),
                "net_exposure_after": (
                    safe_float(short_reduce.get("long_qty_after"))
                    - safe_float(short_reduce.get("short_qty_after"))
                    if short_reduce
                    else None
                ),
                "basket_exit_before": None,
                "basket_exit_after": None,
            }
        )
    return rows


def _active_exit_price(result: BacktestResult) -> float | None:
    for order in result.final_active_orders or []:
        purpose = str(order.get("purpose") or "")
        if purpose == "LONG_TP_EXIT":
            value = order.get("trigger_price")
            if value in (None, ""):
                value = order.get("price")
            return safe_float(value) if value not in (None, "") else None
    for purpose, order in zip(result.final_active_order_purposes or [], result.final_active_orders or []):
        if str(purpose) == "LONG_TP_EXIT" and isinstance(order, dict):
            value = order.get("trigger_price")
            if value not in (None, ""):
                return safe_float(value)
    return None


def exit_rebuild_stats(
    result: BacktestResult,
    window_candles: list[Any] | None = None,
) -> dict[str, Any]:
    history: list[dict[str, Any]] = []
    for row in result.order_log or []:
        purpose = str(row.get("purpose") or "")
        if purpose != "LONG_TP_EXIT":
            continue
        event = str(row.get("event_type") or "").lower()
        if event not in {"submitted", "cancelled"}:
            continue
        history.append(
            {
                "action": "submit" if event == "submitted" else "cancel",
                "timestamp": row.get("timestamp"),
                "candle_index": row.get("candle_index"),
                "exit_price": safe_float(row.get("trigger_price") or row.get("price")),
            }
        )

    by_ts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in history:
        by_ts[str(item["timestamp"])].append(item)

    rebuilds: list[dict[str, Any]] = []
    for ts, items in sorted(by_ts.items()):
        cancels = [item for item in items if item["action"] == "cancel"]
        submits = [item for item in items if item["action"] == "submit"]
        if not (cancels and submits):
            continue
        old_price = cancels[0]["exit_price"]
        new_price = submits[0]["exit_price"]
        rebuilds.append(
            {
                "timestamp": ts,
                "candle_index": submits[0]["candle_index"],
                "old_exit_price": old_price,
                "new_exit_price": new_price,
                "delta_exit": new_price - old_price,
                "is_increase": new_price > old_price + 1e-12,
            }
        )

    harmful = 0
    if window_candles is not None:
        for rebuild in rebuilds:
            old_price = rebuild["old_exit_price"]
            new_price = rebuild["new_exit_price"]
            candle_index = rebuild["candle_index"]
            if candle_index is None or old_price is None or new_price is None:
                continue
            if new_price <= old_price + 1e-12:
                continue
            try:
                start = int(candle_index) + 1
            except (TypeError, ValueError):
                continue
            future = window_candles[start:]
            if not future:
                continue

            def _high(candle: Any) -> float:
                if isinstance(candle, dict):
                    return float(candle.get("high"))
                return float(candle.high)

            max_high = max(_high(candle) for candle in future)
            if max_high >= old_price - 1e-12 and max_high < new_price - 1e-12:
                harmful += 1
                rebuild["replaced_reachable_with_unreachable"] = True
            else:
                rebuild["replaced_reachable_with_unreachable"] = False

    return {
        "exit_rebuild_count": len(rebuilds),
        "exit_increase_count": sum(1 for row in rebuilds if row.get("is_increase")),
        "old_exit_later_reachable_count": harmful,
        "rebuilds": rebuilds,
    }


def analyze_trade(
    result: BacktestResult,
    *,
    variant: str,
    long_add_pct: float,
    target_profit_usdt: float,
    window_candles: list[Any] | None = None,
    valid: bool = True,
    skip_reason: str = "ok",
) -> dict[str, Any]:
    status = normalize_trade_status(result)
    fills = list(result.fill_log or [])
    same_candle = same_candle_long_add_short_reduce(fills)
    exposure = exposure_from_fills(fills)
    rebuild = exit_rebuild_stats(result, window_candles=window_candles)
    coverage_rows = build_pnl_coverage_audit(result) if valid else []
    undercoverage = sum(1 for row in coverage_rows if "undercover" in str(row.get("status") or "").lower())
    pending_final = sum(1 for row in coverage_rows if "pending_final" in str(row.get("status") or "").lower())
    cycle_rows = build_cycle_rows(
        variant=variant,
        long_add_pct=long_add_pct,
        start_index=int(result.start_index or 0),
        fills=fills,
        target_profit_usdt=target_profit_usdt,
    )
    completed_cycles = sum(1 for row in cycle_rows if row.get("complete"))
    max_cycle = max((int(row["cycle_index"]) for row in cycle_rows), default=0)
    if result.cycles_seen is not None:
        max_cycle = max(max_cycle, int(result.cycles_seen))

    realized = safe_float(result.realized_pnl)
    unrealized = safe_float(result.unrealized_pnl)
    mtm = safe_float(result.overall_pnl, realized + unrealized)
    long_qty = safe_float(result.final_long_qty)
    short_qty = safe_float(result.final_short_qty)
    mark = safe_float(result.final_price)
    active_exit = _active_exit_price(result)
    distance_to_exit = (active_exit - mark) if active_exit is not None and mark > 0 else None
    duration = int(result.candles_processed or 0)
    negative_closed = bool(status == "closed" and realized < 0)

    # Attach basket exit levels around cycles from rebuild timeline when possible.
    rebuilds = rebuild.get("rebuilds") or []
    for row in cycle_rows:
        long_ci = None
        short_ci = None
        for fill in fills:
            purpose = str(fill.get("purpose") or "")
            if purpose == f"CYCLE_{row['cycle_index']}_LONG_ADD":
                long_ci = fill.get("candle_index")
            if purpose == f"CYCLE_{row['cycle_index']}_SHORT_REDUCE":
                short_ci = fill.get("candle_index")
        before = None
        after = None
        for item in rebuilds:
            ci = item.get("candle_index")
            if ci is None:
                continue
            try:
                ci_i = int(ci)
            except (TypeError, ValueError):
                continue
            if long_ci is not None:
                try:
                    if ci_i <= int(long_ci):
                        before = item.get("new_exit_price")
                except (TypeError, ValueError):
                    pass
            if short_ci is not None:
                try:
                    if ci_i <= int(short_ci):
                        after = item.get("new_exit_price")
                except (TypeError, ValueError):
                    pass
        row["basket_exit_before"] = before
        row["basket_exit_after"] = after

    return {
        "variant": variant,
        "long_add_pct": long_add_pct,
        "start_index": int(result.start_index or 0),
        "start_timestamp": result.start_time.isoformat() if result.start_time else "",
        "end_timestamp": result.end_time.isoformat() if result.end_time else "",
        "valid": valid,
        "skip_reason": skip_reason,
        "status": status,
        "duration_candles": duration,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "mtm_pnl": mtm,
        "max_cycle": max_cycle,
        "completed_cycles": completed_cycles,
        "negative_closed_trade": int(negative_closed),
        "undercoverage": undercoverage,
        "pending_final_exit": pending_final,
        "final_long_qty": long_qty,
        "final_short_qty": short_qty,
        "final_net_qty": long_qty - short_qty,
        "final_long_avg": safe_float(result.final_long_avg_price),
        "final_short_avg": safe_float(result.final_short_avg_price),
        "mark_price_end": mark,
        "active_exit_price": active_exit,
        "distance_to_exit": distance_to_exit,
        "max_long_notional": exposure["max_long_notional"],
        "max_short_notional": exposure["max_short_notional"],
        "max_total_notional": exposure["max_total_notional"],
        "max_abs_net_exposure": exposure["max_abs_net_exposure"],
        "fees": exposure["fees"],
        "exit_rebuild_count": rebuild["exit_rebuild_count"],
        "exit_increase_count": rebuild["exit_increase_count"],
        "old_exit_later_reachable_count": rebuild["old_exit_later_reachable_count"],
        "same_candle_long_add_short_reduce": len(same_candle),
        "same_candle_cases": same_candle,
        "cycle_rows": cycle_rows,
        "applied_long_fill_distance_pct": safe_float(result.long_fill_distance_pct, long_add_pct),
        "applied_target_profit_usdt": safe_float(result.target_profit_usdt, target_profit_usdt),
        "applied_tp_profit_target_pct": safe_float(result.tp_profit_target_pct),
        "exit_reason": result.exit_reason,
        "error": result.error,
    }


def aggregate_variant_trades(
    trades: list[dict[str, Any]],
    *,
    planned_starts: int,
    skipped: list[dict[str, Any]],
) -> dict[str, Any]:
    valid = [row for row in trades if row.get("valid")]
    closed = [row for row in valid if row.get("status") == "closed"]
    open_rows = [row for row in valid if row.get("status") == "open"]
    closed_pnls = [safe_float(row.get("realized_pnl")) for row in closed]
    mtm_pnls = [safe_float(row.get("mtm_pnl")) for row in valid]
    durations = [safe_float(row.get("duration_candles")) for row in valid]
    cycles = [safe_float(row.get("max_cycle")) for row in valid]
    nets = [safe_float(row.get("max_abs_net_exposure")) for row in valid]
    notionals = [safe_float(row.get("max_total_notional")) for row in valid]
    fees = [safe_float(row.get("fees")) for row in valid]
    long_runners = [
        row
        for row in open_rows
        if safe_float(row.get("duration_candles")) >= 1000
    ]

    def share(threshold: int) -> float:
        if not valid:
            return 0.0
        return sum(1 for row in valid if safe_float(row.get("duration_candles")) > threshold) / len(valid)

    return {
        "planned_starts": planned_starts,
        "valid_trades": len(valid),
        "skipped_starts": len(skipped),
        "closed_trades": len(closed),
        "open_trades": len(open_rows),
        "closed_rate": (len(closed) / len(valid)) if valid else 0.0,
        "negative_closed_trades": sum(int(row.get("negative_closed_trade") or 0) for row in closed),
        "undercoverage": sum(int(row.get("undercoverage") or 0) for row in valid),
        "same_candle_violations": sum(int(row.get("same_candle_long_add_short_reduce") or 0) for row in valid),
        "sum_closed_pnl": sum(closed_pnls),
        "avg_closed_pnl": statistics.mean(closed_pnls) if closed_pnls else None,
        "median_closed_pnl": statistics.median(closed_pnls) if closed_pnls else None,
        "sum_mtm_pnl": sum(mtm_pnls),
        "avg_mtm_pnl": statistics.mean(mtm_pnls) if mtm_pnls else None,
        "median_mtm_pnl": statistics.median(mtm_pnls) if mtm_pnls else None,
        "mtm_p10": percentile(mtm_pnls, 10),
        "mtm_p25": percentile(mtm_pnls, 25),
        "mtm_p50": percentile(mtm_pnls, 50),
        "mtm_p75": percentile(mtm_pnls, 75),
        "mtm_p90": percentile(mtm_pnls, 90),
        "worst_trade_mtm": min(mtm_pnls) if mtm_pnls else None,
        "best_trade_mtm": max(mtm_pnls) if mtm_pnls else None,
        "avg_duration_candles": statistics.mean(durations) if durations else None,
        "max_duration_candles": max(durations) if durations else None,
        "share_duration_gt_1000": share(1000),
        "share_duration_gt_3000": share(3000),
        "share_duration_gt_5000": share(5000),
        "open_long_runner_count": len(long_runners),
        "open_long_runner_share": (len(long_runners) / len(valid)) if valid else 0.0,
        "avg_max_cycle": statistics.mean(cycles) if cycles else None,
        "max_cycle": max(cycles) if cycles else None,
        "avg_max_abs_net_exposure": statistics.mean(nets) if nets else None,
        "max_abs_net_exposure": max(nets) if nets else None,
        "avg_max_total_notional": statistics.mean(notionals) if notionals else None,
        "max_total_notional": max(notionals) if notionals else None,
        "avg_fees": statistics.mean(fees) if fees else None,
        "old_exit_later_reachable_count": sum(
            int(row.get("old_exit_later_reachable_count") or 0) for row in valid
        ),
        "exit_rebuild_count": sum(int(row.get("exit_rebuild_count") or 0) for row in valid),
        "exit_increase_count": sum(int(row.get("exit_increase_count") or 0) for row in valid),
        "pending_final_exit": sum(int(row.get("pending_final_exit") or 0) for row in valid),
    }


def paired_compare_to_baseline(
    trades_by_variant: dict[float, list[dict[str, Any]]],
    *,
    baseline_pct: float = BASELINE_LONG_ADD_PCT,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    baseline_rows = {
        int(row["start_index"]): row
        for row in trades_by_variant.get(baseline_pct, [])
        if row.get("valid")
    }
    paired_rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []

    for long_add_pct, trades in sorted(trades_by_variant.items()):
        if abs(long_add_pct - baseline_pct) < 1e-12:
            continue
        better = worse = equal = 0
        mtm_diffs: list[float] = []
        extra_closed = 0
        extra_long_runners = 0
        for row in trades:
            if not row.get("valid"):
                continue
            start_index = int(row["start_index"])
            base = baseline_rows.get(start_index)
            if base is None:
                continue
            mtm_diff = safe_float(row.get("mtm_pnl")) - safe_float(base.get("mtm_pnl"))
            mtm_diffs.append(mtm_diff)
            if abs(mtm_diff) < 1e-12:
                equal += 1
            elif mtm_diff > 0:
                better += 1
            else:
                worse += 1
            variant_closed = row.get("status") == "closed"
            base_closed = base.get("status") == "closed"
            variant_open = row.get("status") == "open"
            base_open = base.get("status") == "open"
            if variant_closed and not base_closed:
                extra_closed += 1
            variant_runner = variant_open and safe_float(row.get("duration_candles")) >= 1000
            base_runner = base_open and safe_float(base.get("duration_candles")) >= 1000
            if variant_runner and not base_runner:
                extra_long_runners += 1
            neg_variant = bool(row.get("negative_closed_trade"))
            neg_base = bool(base.get("negative_closed_trade"))
            paired_rows.append(
                {
                    "start_index": start_index,
                    "variant": row.get("variant"),
                    "long_add_pct": long_add_pct,
                    "baseline_long_add_pct": baseline_pct,
                    "mtm_diff_vs_0_5": mtm_diff,
                    "variant_status": row.get("status"),
                    "baseline_status": base.get("status"),
                    "duration_diff": safe_float(row.get("duration_candles"))
                    - safe_float(base.get("duration_candles")),
                    "max_exposure_diff": safe_float(row.get("max_abs_net_exposure"))
                    - safe_float(base.get("max_abs_net_exposure")),
                    "cycle_diff": safe_float(row.get("max_cycle")) - safe_float(base.get("max_cycle")),
                    "closes_while_baseline_open": int(variant_closed and base_open),
                    "open_while_baseline_closed": int(variant_open and base_closed),
                    "prevents_negative_closed": int(neg_base and not neg_variant),
                    "creates_negative_closed": int(neg_variant and not neg_base),
                    "variant_mtm": row.get("mtm_pnl"),
                    "baseline_mtm": base.get("mtm_pnl"),
                }
            )
        summary_rows.append(
            {
                "variant": variant_dir_name(long_add_pct),
                "long_add_pct": long_add_pct,
                "baseline_long_add_pct": baseline_pct,
                "better_starts": better,
                "worse_starts": worse,
                "equal_starts": equal,
                "median_mtm_diff": statistics.median(mtm_diffs) if mtm_diffs else None,
                "avg_mtm_diff": statistics.mean(mtm_diffs) if mtm_diffs else None,
                "extra_closed_trades": extra_closed,
                "extra_long_runners": extra_long_runners,
            }
        )
    return paired_rows, summary_rows


def rank_variants(summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Priority: causality → undercoverage → negative closed → total MTM →
    # fewer long-runners → better worst MTM → lower max exposure → closed rate → closed PnL.
    ranked = sorted(
        summaries,
        key=lambda row: (
            int(row.get("same_candle_violations") or 0),
            int(row.get("undercoverage") or 0),
            int(row.get("negative_closed_trades") or 0),
            -safe_float(row.get("sum_mtm_pnl")),
            int(row.get("open_long_runner_count") or 0),
            -safe_float(row.get("worst_trade_mtm"), -1e18),
            safe_float(row.get("max_abs_net_exposure")),
            -safe_float(row.get("closed_rate")),
            -safe_float(row.get("sum_closed_pnl")),
        ),
    )
    out: list[dict[str, Any]] = []
    for idx, row in enumerate(ranked, start=1):
        payload = dict(row)
        payload["rank"] = idx
        out.append(payload)
    return out

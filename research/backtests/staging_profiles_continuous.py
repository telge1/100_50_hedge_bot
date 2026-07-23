"""Chronological continuous sequencer for second-leg staging profile comparison.

One trade at a time per coin×profile. Next entry only after flat + orders done.
Research-only; no multi-start windows, no FULL_DYNAMIC, no live integration.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from research.backtests.inventory_mtm_freeze import safe_float
from research.backtests.recovery_reentry_policy import min_next_start_index
from research.backtests.run_two_early_medium_candidate_validation import _run_one

ALLOWED_PROFILES = ("legacy", "two_early_medium", "adaptive_equal")
FORBIDDEN_PROFILES = (
    "fixed_step_1pct_equal",
    "fixed_step_2pct_equal",
    "adaptive_backloaded",
    "two_early_medium_full_dynamic",
)


@dataclass(frozen=True)
class ContinuousTradeBounds:
    trade_id: str
    start_bar: int
    end_bar: int
    flat_bar: int | None
    next_start_bar: int | None
    is_flat: bool


def trade_id(coin: str, profile: str, trade_number: int) -> str:
    return f"{str(coin).upper()}|{profile}|continuous|{int(trade_number):04d}"


def trade_end_bar(*, start_index: int, candles_processed: int) -> int:
    """Absolute end bar matching continuous_reentry (start + processed)."""
    return int(start_index) + int(candles_processed or 0)


def next_start_after_flat(flat_or_end_bar: int) -> int:
    return min_next_start_index(int(flat_or_end_bar))


def validate_profiles(profiles: Sequence[str]) -> tuple[str, ...]:
    out: list[str] = []
    for p in profiles:
        name = str(p).strip()
        if not name:
            continue
        if name in FORBIDDEN_PROFILES or name.endswith("_full_dynamic") or name.startswith(
            "fixed_step_"
        ):
            raise ValueError(f"forbidden profile for continuous staging run: {name}")
        if name not in ALLOWED_PROFILES:
            raise ValueError(
                f"unsupported profile {name!r}; allowed={list(ALLOWED_PROFILES)}"
            )
        if name not in out:
            out.append(name)
    if not out:
        raise ValueError("no profiles selected")
    return tuple(out)


def run_one_trade(
    *,
    coin: str,
    trade_number: int,
    start_index: int,
    profile: str,
    candles: list[Any],
    capture_economics: bool = True,
) -> dict[str, Any]:
    """Run a single isolated trade from absolute start_index to flat or series end."""
    row = _run_one(
        coin=coin,
        trade_number=trade_number,
        start_index=int(start_index),
        profile=profile,
        candles=candles,
        baseline_row=None,
        capture_economics=capture_economics,
    )
    processed = int(row.get("candles_processed") or row.get("duration_candles") or 0)
    end_bar = trade_end_bar(start_index=start_index, candles_processed=processed)
    flat = int(row.get("trade_flat") or 0) == 1
    flat_bar = end_bar if flat else None
    next_start = next_start_after_flat(end_bar) if flat else None
    long_q = safe_float(row.get("final_long_qty"))
    short_q = safe_float(row.get("final_short_qty"))
    active_pos = (abs(long_q) > 1e-12) or (abs(short_q) > 1e-12)
    tid = trade_id(coin, profile, trade_number)
    realized = safe_float(row.get("realized_pnl"))
    open_mtm = 0.0 if flat else safe_float(row.get("open_mtm"))
    total = realized + open_mtm
    row.update(
        {
            "trade_id": tid,
            "profile": profile,
            "start_bar": int(start_index),
            "end_bar": int(end_bar),
            "flat_bar": flat_bar,
            "next_start_bar": next_start,
            "duration_candles": processed,
            "candles_processed": processed,
            "is_blocker": int((not flat) and active_pos),
            "active_position_at_end": int(active_pos),
            "realized_pnl": realized,
            "open_mtm": open_mtm,
            "total_pnl": total,
            "closed_pnl": realized if flat else 0.0,
            "pnl_reconcile_ok": int(abs(total - (realized + open_mtm)) < 1e-9),
        }
    )
    return row


TradeRunner = Callable[..., dict[str, Any]]


def run_continuous_sequence(
    *,
    coin: str,
    profile: str,
    candles: list[Any],
    warmup: int,
    max_trades: int | None = None,
    capture_economics: bool = True,
    trade_runner: TradeRunner | None = None,
) -> list[dict[str, Any]]:
    """Chronologically chain trades: next start only after prior flat."""
    runner = trade_runner or run_one_trade
    n = len(candles)
    start_index = max(0, int(warmup))
    trades: list[dict[str, Any]] = []
    trade_number = 0

    while start_index < n:
        if max_trades is not None and trade_number >= int(max_trades):
            break
        remaining = n - start_index
        if remaining <= 1:
            break

        trade_number += 1
        row = runner(
            coin=coin,
            trade_number=trade_number,
            start_index=start_index,
            profile=profile,
            candles=candles,
            capture_economics=capture_economics,
        )
        trades.append(row)

        if int(row.get("trade_flat") or 0) != 1:
            # Open / blocker at data end — stop chain.
            break

        nxt = row.get("next_start_bar")
        if nxt is None:
            break
        nxt_i = int(nxt)
        if nxt_i <= int(row.get("flat_bar") or row.get("end_bar") or start_index):
            raise RuntimeError(
                f"invariant violated: next_start_bar={nxt_i} must be > flat/end "
                f"for {row.get('trade_id')}"
            )
        if nxt_i >= n:
            break
        start_index = nxt_i

    return trades


def check_overlap_integrity(trades: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-trade integrity rows; hard gates encoded as flags."""
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for i, trade in enumerate(trades):
        coin = str(trade.get("coin") or "")
        profile = str(trade.get("profile") or "")
        tid = str(trade.get("trade_id") or "")
        start_bar = int(trade.get("start_bar") or trade.get("start_index") or 0)
        end_bar = int(trade.get("end_bar") or 0)
        flat_bar = trade.get("flat_bar")
        flat_bar_i = int(flat_bar) if flat_bar is not None else None
        next_start = trade.get("next_start_bar")
        next_start_i = int(next_start) if next_start is not None else None

        overlap = 0
        if i > 0:
            prev = trades[i - 1]
            prev_end = int(prev.get("end_bar") or 0)
            prev_flat = prev.get("flat_bar")
            prev_flat_i = int(prev_flat) if prev_flat is not None else None
            # Overlap if this start is within previous open interval [prev_start, prev_end]
            if start_bar <= prev_end:
                overlap = 1
            if prev_flat_i is not None and start_bar <= prev_flat_i:
                overlap = 1
            if int(prev.get("trade_flat") or 0) != 1:
                overlap = 1

        dup_id = int(tid in seen_ids)
        seen_ids.add(tid)

        stale_orders = int(
            safe_float(trade.get("orphan_stage_order"))
            or safe_float(trade.get("late_stage_fill_after_exit"))
            or 0
        )
        # At next start of following trade, prior must be flat with no position.
        active_at_next = 0
        if i + 1 < len(trades):
            if int(trade.get("trade_flat") or 0) != 1:
                active_at_next = 1
            if int(trade.get("active_position_at_end") or 0) == 1:
                active_at_next = 1

        # next_start strict after flat
        next_ok = 1
        if flat_bar_i is not None and next_start_i is not None:
            if next_start_i <= flat_bar_i:
                next_ok = 0
                overlap = 1

        passed = int(
            overlap == 0
            and stale_orders == 0
            and active_at_next == 0
            and dup_id == 0
            and next_ok == 1
            and start_bar >= 0
            and end_bar >= start_bar
        )
        rows.append(
            {
                "coin": coin,
                "profile": profile,
                "trade_id": tid,
                "start_bar": start_bar,
                "end_bar": end_bar,
                "flat_bar": flat_bar_i if flat_bar_i is not None else "",
                "next_start_bar": next_start_i if next_start_i is not None else "",
                "overlap_detected": overlap,
                "stale_orders_detected": stale_orders,
                "active_position_at_next_start": active_at_next,
                "duplicate_trade_id": dup_id,
                "pass": passed,
            }
        )
    return rows


def first_trade_parity_rows(
    by_profile: dict[str, list[dict[str, Any]]],
    *,
    coin: str,
) -> dict[str, Any]:
    """Confirm first trade start_bar identical across profiles for one coin."""
    starts = {
        p: (rows[0].get("start_bar") if rows else None) for p, rows in by_profile.items()
    }
    values = [v for v in starts.values() if v is not None]
    ok = len(set(int(v) for v in values)) <= 1 and len(values) == len(by_profile)
    return {
        "coin": coin,
        "first_start_by_profile": starts,
        "first_trade_start_parity_ok": int(ok),
    }


def classify_blocker_root_cause(trade: dict[str, Any]) -> str:
    if int(trade.get("is_blocker") or 0) != 1 and int(trade.get("trade_flat") or 0) == 1:
        return ""
    filled = int(safe_float(trade.get("filled_stages")))
    planned = int(safe_float(trade.get("planned_stages")))
    max_cycle = int(safe_float(trade.get("max_cycle")))
    bounce = int(safe_float(trade.get("bounce_reaches_exit")))
    recovery = int(safe_float(trade.get("recovery_active")))
    dist = trade.get("distance_to_exit_pct")
    dist_f = safe_float(dist) if dist not in (None, "") else None

    if recovery:
        return "recovery_reload"
    if max_cycle >= 5:
        return "high_cycle"
    if planned >= 2 and filled <= 0:
        return "no_second_leg_fill"
    if planned >= 2 and filled > 0 and filled < planned:
        return "residual_stage_too_far"
    if dist_f is not None and dist_f > 2.0:
        return "basket_exit_too_far"
    if bounce == 0 and planned >= 1:
        return "insufficient_price_revisit"
    if str(trade.get("exit_reason") or "") in {
        "series_end_with_open_positions",
        "max_candles_reached",
    }:
        return "data_end"
    return "other"


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * (pct / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return xs[int(k)]
    return xs[f] * (c - k) + xs[c] * (k - f)


def _profit_factor(closed_pnls: list[float]) -> float | None:
    gains = sum(x for x in closed_pnls if x > 0)
    losses = sum(-x for x in closed_pnls if x < 0)
    if losses <= 1e-12:
        return None if gains <= 1e-12 else float("inf")
    return gains / losses


def _max_losing_streak(closed_pnls: list[float]) -> int:
    best = 0
    cur = 0
    for x in closed_pnls:
        if x < 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def summarize_coin_profile(
    trades: Sequence[dict[str, Any]],
    *,
    coin: str,
    profile: str,
    n_candles: int,
    first_ts: str | None = None,
    last_ts: str | None = None,
) -> dict[str, Any]:
    started = len(trades)
    flat = [t for t in trades if int(t.get("trade_flat") or 0) == 1]
    open_end = [t for t in trades if int(t.get("trade_flat") or 0) != 1]
    blockers = [t for t in trades if int(t.get("is_blocker") or 0) == 1]
    closed_pnls = [safe_float(t.get("realized_pnl")) for t in flat]
    durations = [int(t.get("duration_candles") or 0) for t in trades]
    flat_durs = [int(t.get("duration_candles") or 0) for t in flat]

    sum_realized = sum(safe_float(t.get("realized_pnl")) for t in trades)
    sum_open = sum(safe_float(t.get("open_mtm")) for t in trades)
    total = sum_realized + sum_open

    covered = sum(
        1
        for t in flat
        if str(t.get("coverage_class") or "")
        in {"covered_by_second_leg", "covered_by_basket_exit"}
    )
    econ_uc = sum(int(t.get("economic_undercoverage_closed") or 0) for t in trades)
    suf_false = sum(int(t.get("sufficient_false_closed") or 0) for t in trades)
    neg_flat = sum(1 for x in closed_pnls if x < 0)
    pos_flat = sum(1 for x in closed_pnls if x > 0)

    wins = sum(1 for x in closed_pnls if x > 0)
    win_rate = (wins / len(closed_pnls)) if closed_pnls else None

    stage_fills = sum(int(safe_float(t.get("filled_stages"))) for t in trades)
    orders = sum(int(safe_float(t.get("orders_submitted") or t.get("orders"))) for t in trades)
    cancels = sum(
        int(safe_float(t.get("cancel_count") or t.get("cancels") or len(t.get("cancelled_stage_indices") or [])))
        for t in trades
    )
    min_nf = sum(int(safe_float(t.get("min_notional_fallbacks"))) for t in trades)
    recovery = sum(int(safe_float(t.get("recovery_active"))) for t in trades)
    max_cycle = max((int(safe_float(t.get("max_cycle"))) for t in trades), default=0)

    blocker_open = sum(safe_float(t.get("open_mtm")) for t in blockers)
    blocker_realized = sum(safe_float(t.get("realized_pnl")) for t in blockers)
    blocker_durs = [int(t.get("duration_candles") or 0) for t in blockers]

    drawdowns = [safe_float(t.get("max_drawdown_pct")) for t in trades]
    worst_dd = min(drawdowns) if drawdowns else 0.0

    return {
        "coin": coin,
        "profile": profile,
        "n_candles": n_candles,
        "first_timestamp": first_ts or "",
        "last_timestamp": last_ts or "",
        "trades_started": started,
        "trades_flat_closed": len(flat),
        "trades_open_at_data_end": len(open_end),
        "close_rate": (len(flat) / started) if started else 0.0,
        "successful_covered_closes": covered,
        "economic_undercoverage_closed": econ_uc,
        "sufficient_false_closed": suf_false,
        "negative_flat_closes": neg_flat,
        "positive_flat_closes": pos_flat,
        "sum_realized_pnl": sum_realized,
        "sum_open_mtm": sum_open,
        "total_pnl": total,
        "average_total_pnl_per_started_trade": (total / started) if started else 0.0,
        "average_closed_pnl": statistics.mean(closed_pnls) if closed_pnls else None,
        "median_closed_pnl": statistics.median(closed_pnls) if closed_pnls else None,
        "best_closed_trade": max(closed_pnls) if closed_pnls else None,
        "worst_closed_trade": min(closed_pnls) if closed_pnls else None,
        "profit_factor_closed": _profit_factor(closed_pnls),
        "win_rate_closed": win_rate,
        "avg_hold_candles": statistics.mean(durations) if durations else None,
        "median_hold_candles": statistics.median(flat_durs) if flat_durs else (
            statistics.median(durations) if durations else None
        ),
        "p90_hold_candles": _percentile(durations, 90),
        "max_hold_candles": max(durations) if durations else 0,
        "max_losing_streak_closed": _max_losing_streak(closed_pnls),
        "max_drawdown_pct": worst_dd,
        "end_exposure": safe_float(open_end[-1].get("gross_exposure")) if open_end else 0.0,
        "highest_cycle": max_cycle,
        "blocker_count": len(blockers),
        "blocker_rate": (len(blockers) / started) if started else 0.0,
        "blocker_open_mtm": blocker_open,
        "blocker_realized_pnl": blocker_realized,
        "blocker_total_pnl": blocker_realized + blocker_open,
        "avg_blocker_duration": statistics.mean(blocker_durs) if blocker_durs else None,
        "highest_blocker_cycle": max(
            (int(safe_float(t.get("max_cycle"))) for t in blockers), default=0
        ),
        "recovery_reloads": recovery,
        "stage_fills": stage_fills,
        "orders": orders,
        "cancels": cancels,
        "min_notional_fallbacks": min_nf,
        "invalid_partial": sum(int(safe_float(t.get("invalid_partial"))) for t in trades),
        "over_close": sum(int(safe_float(t.get("over_close"))) for t in trades),
        "duplicate_stage": sum(int(safe_float(t.get("duplicate_stage"))) for t in trades),
        "orphan_stage_order": sum(int(safe_float(t.get("orphan_stage_order"))) for t in trades),
        "stale_generation_fill": sum(
            int(safe_float(t.get("stale_generation_fill"))) for t in trades
        ),
        "late_stage_fill_after_exit": sum(
            int(safe_float(t.get("late_stage_fill_after_exit"))) for t in trades
        ),
        "pnl_per_1000_candles": (total / n_candles * 1000.0) if n_candles else None,
        "blocker_per_100_trades": (len(blockers) / started * 100.0) if started else None,
    }


def summarize_by_profile(coin_rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    by_p: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in coin_rows:
        by_p[str(r["profile"])].append(r)
    out: list[dict[str, Any]] = []
    for profile, rows in sorted(by_p.items()):
        totals = {
            "profile": profile,
            "n_coins": len(rows),
            "trades_started": sum(int(r["trades_started"]) for r in rows),
            "trades_flat_closed": sum(int(r["trades_flat_closed"]) for r in rows),
            "trades_open_at_data_end": sum(int(r["trades_open_at_data_end"]) for r in rows),
            "sum_realized_pnl": sum(safe_float(r["sum_realized_pnl"]) for r in rows),
            "sum_open_mtm": sum(safe_float(r["sum_open_mtm"]) for r in rows),
            "total_pnl": sum(safe_float(r["total_pnl"]) for r in rows),
            "blocker_count": sum(int(r["blocker_count"]) for r in rows),
            "economic_undercoverage_closed": sum(
                int(r["economic_undercoverage_closed"]) for r in rows
            ),
            "sufficient_false_closed": sum(int(r["sufficient_false_closed"]) for r in rows),
            "invalid_partial": sum(int(r["invalid_partial"]) for r in rows),
            "over_close": sum(int(r["over_close"]) for r in rows),
            "duplicate_stage": sum(int(r["duplicate_stage"]) for r in rows),
            "orphan_stage_order": sum(int(r["orphan_stage_order"]) for r in rows),
            "stage_fills": sum(int(r["stage_fills"]) for r in rows),
            "orders": sum(int(r["orders"]) for r in rows),
            "cancels": sum(int(r["cancels"]) for r in rows),
            "equal_coin_mean_total_pnl": statistics.mean(
                [safe_float(r["total_pnl"]) for r in rows]
            )
            if rows
            else None,
            "equal_coin_median_total_pnl": statistics.median(
                [safe_float(r["total_pnl"]) for r in rows]
            )
            if rows
            else None,
            "max_drawdown_pct_worst_coin": min(
                (safe_float(r["max_drawdown_pct"]) for r in rows), default=0.0
            ),
        }
        started = totals["trades_started"]
        totals["close_rate"] = (
            totals["trades_flat_closed"] / started if started else 0.0
        )
        totals["blocker_rate"] = (
            totals["blocker_count"] / started if started else 0.0
        )
        totals["avg_total_pnl_per_trade"] = (
            totals["total_pnl"] / started if started else 0.0
        )
        out.append(totals)
    return out


def leave_one_coin_out(coin_rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    by_p: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in coin_rows:
        by_p[str(r["profile"])].append(r)
    out: list[dict[str, Any]] = []
    for profile, rows in sorted(by_p.items()):
        full = sum(safe_float(r["total_pnl"]) for r in rows)
        for leave in rows:
            coin = str(leave["coin"])
            partial = full - safe_float(leave["total_pnl"])
            out.append(
                {
                    "profile": profile,
                    "left_out_coin": coin,
                    "total_pnl_without_coin": partial,
                    "left_out_coin_total_pnl": safe_float(leave["total_pnl"]),
                    "full_total_pnl": full,
                }
            )
        # without best / worst
        if rows:
            best = max(rows, key=lambda r: safe_float(r["total_pnl"]))
            worst = min(rows, key=lambda r: safe_float(r["total_pnl"]))
            out.append(
                {
                    "profile": profile,
                    "left_out_coin": f"BEST:{best['coin']}",
                    "total_pnl_without_coin": full - safe_float(best["total_pnl"]),
                    "left_out_coin_total_pnl": safe_float(best["total_pnl"]),
                    "full_total_pnl": full,
                }
            )
            out.append(
                {
                    "profile": profile,
                    "left_out_coin": f"WORST:{worst['coin']}",
                    "total_pnl_without_coin": full - safe_float(worst["total_pnl"]),
                    "left_out_coin_total_pnl": safe_float(worst["total_pnl"]),
                    "full_total_pnl": full,
                }
            )
    return out


def safety_aggregate(trades: Iterable[dict[str, Any]], integrity: Iterable[dict[str, Any]]) -> dict[str, Any]:
    trades_l = list(trades)
    integ = list(integrity)
    gates = {
        "economic_undercoverage_closed": sum(
            int(t.get("economic_undercoverage_closed") or 0) for t in trades_l
        ),
        "sufficient_false_closed": sum(
            int(t.get("sufficient_false_closed") or 0) for t in trades_l
        ),
        "invalid_partial": sum(int(safe_float(t.get("invalid_partial"))) for t in trades_l),
        "over_close": sum(int(safe_float(t.get("over_close"))) for t in trades_l),
        "duplicate_stage": sum(int(safe_float(t.get("duplicate_stage"))) for t in trades_l),
        "orphan_stage_order": sum(int(safe_float(t.get("orphan_stage_order"))) for t in trades_l),
        "stale_generation_fill": sum(
            int(safe_float(t.get("stale_generation_fill"))) for t in trades_l
        ),
        "late_stage_fill_after_exit": sum(
            int(safe_float(t.get("late_stage_fill_after_exit"))) for t in trades_l
        ),
        "overlap_detected": sum(int(r.get("overlap_detected") or 0) for r in integ),
        "stale_orders_detected": sum(int(r.get("stale_orders_detected") or 0) for r in integ),
        "active_position_at_next_start": sum(
            int(r.get("active_position_at_next_start") or 0) for r in integ
        ),
        "integrity_fail_rows": sum(1 for r in integ if int(r.get("pass") or 0) != 1),
        "pnl_reconcile_fail": sum(
            1 for t in trades_l if int(t.get("pnl_reconcile_ok") or 0) != 1
        ),
    }
    gates["all_green"] = int(all(v == 0 for k, v in gates.items() if k != "all_green"))
    return gates

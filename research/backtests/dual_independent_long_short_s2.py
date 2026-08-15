"""Research helpers for dual independent Long/Short S2 continuous audit.

Pure functions only — no live/runtime mutation.
"""

from __future__ import annotations

import statistics
from collections import Counter
from typing import Any

from research.backtests.backtest_report import BacktestResult
from research.backtests.debug_report import calculate_unrealized_pnl
from research.backtests.inventory_mtm_freeze import InventoryMtmFreezeConfig, is_injusdt_trade8_undercoverage
from research.backtests.long_add_multistart_metrics import analyze_trade, normalize_trade_status, safe_float
from research.backtests.safe_cycle_boundary_freeze import (
    detect_invalid_partial_cycle,
    is_direction_aware_cycle_opener,
    is_direction_aware_second_leg,
)

# Prior S2 (B1 terminal) parity targets from safe_cycle_boundary_freeze_audit_20260720.
S2_REFERENCE_RECOVERED = 24
S2_REFERENCE_SERIES_MTM = 42.06951304045645
S2_REFERENCE_TRADES = 264
S2_REFERENCE_CLOSED = 261
S2_REFERENCE_BLOCKERS = 3
S2_MTM_TOLERANCE = 1.0

LONG_INITIAL_NOTIONAL_USDT = 100.0
SHORT_INITIAL_NOTIONAL_USDT = 50.0


def build_s2_freeze_config() -> InventoryMtmFreezeConfig:
    return InventoryMtmFreezeConfig(
        variant="A1",
        threshold_usdt=-0.50,
        use_mtm_trigger=False,
        safe_cycle_boundary=True,
        safe_boundary_arm_mode="stop_after_cycle",
        stop_after_cycle=1,
        safe_boundary_variant="S2",
    )


def _ts(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def trade_row_from_result(
    *,
    coin: str,
    side: str,
    variant: str,
    result: BacktestResult,
    candles: list[Any],
    long_add_pct: float,
    target_profit_usdt: float,
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
    freeze_state = dict(excerpt.get("inventory_mtm_freeze_state") or {})
    safe_boundary = dict(freeze_state.get("safe_boundary") or {})
    strategy_excerpt = dict(excerpt.get("strategy_state") or excerpt)
    invalid_partial = int(
        detect_invalid_partial_cycle(strategy_excerpt) if status != "closed" else False
    )
    mtm = safe_float(analysis.get("mtm_pnl"))
    realized = safe_float(analysis.get("realized_pnl"))
    return {
        "coin": coin,
        "side": side,
        "variant": variant,
        "trade_number": int(result.trade_number or 0),
        "direction": side,
        "start_index": start_index,
        "end_index": result.end_index,
        "start_timestamp": _ts(
            getattr(result, "start_time", None)
            or (candles[start_index].timestamp if candles else None)
        ),
        "end_timestamp": _ts(getattr(result, "end_time", None)),
        "status": status,
        "is_blocker": int(status != "closed"),
        "duration_candles": int(result.candles_processed or 0),
        "realized_pnl": realized,
        "unrealized_pnl": analysis.get("unrealized_pnl"),
        "mtm_pnl": mtm,
        "closed_pnl_usdt": realized if status == "closed" else 0.0,
        "final_open_mtm_usdt": mtm if status != "closed" else 0.0,
        "max_cycle": analysis.get("max_cycle"),
        "final_long_qty": analysis.get("final_long_qty"),
        "final_short_qty": analysis.get("final_short_qty"),
        "undercoverage": analysis.get("undercoverage"),
        "exit_reason": result.exit_reason,
        "entry_price": getattr(result, "entry_price", None),
        "base_notional_usdt": getattr(result, "base_notional_usdt", None),
        "injusdt_trade8_marker": int(
            is_injusdt_trade8_undercoverage(coin=coin, trade_number=int(result.trade_number or 0))
        ),
        "freeze_state": safe_boundary.get("freeze_state") or freeze_state.get("cycle_freeze_enabled"),
        "freeze_activated_after_cycle": safe_boundary.get("freeze_activated_after_cycle"),
        "blocked_opener_count": safe_boundary.get("blocked_opener_count") or 0,
        "invalid_partial_cycle": invalid_partial,
        "recovered_flat_of_target_blocker": bool(excerpt.get("recovered_flat_of_target_blocker")),
        "research_terminal_reason": excerpt.get("research_terminal_reason"),
        "fill_log": list(result.fill_log or []),
    }


def summarize_side_trades(rows: list[dict[str, Any]], *, side: str) -> dict[str, Any]:
    closed = [r for r in rows if not r.get("is_blocker")]
    open_rows = [r for r in rows if r.get("is_blocker")]
    closed_pnls = [safe_float(r.get("closed_pnl_usdt")) for r in closed]
    durations = [int(r.get("duration_candles") or 0) for r in rows]
    cycles = [int(safe_float(r.get("max_cycle")) or 0) for r in rows]
    open_mtms = [safe_float(r.get("final_open_mtm_usdt") or r.get("mtm_pnl")) for r in open_rows]
    return {
        "side": side,
        "trades_started": len(rows),
        "trades_closed": len(closed),
        "closed_positive_count": sum(1 for p in closed_pnls if p > 1e-9),
        "closed_negative_count": sum(1 for p in closed_pnls if p < -1e-9),
        "open_blocker_count": len(open_rows),
        "closed_pnl_usdt": sum(closed_pnls),
        "final_open_mtm_usdt": sum(open_mtms),
        "total_series_mtm_usdt": sum(safe_float(r.get("mtm_pnl")) for r in rows),
        "avg_closed_pnl": statistics.fmean(closed_pnls) if closed_pnls else 0.0,
        "median_closed_pnl": float(statistics.median(closed_pnls)) if closed_pnls else None,
        "avg_duration_candles": statistics.fmean(durations) if durations else 0.0,
        "median_duration_candles": float(statistics.median(durations)) if durations else None,
        "cycle_distribution": dict(sorted(Counter(cycles).items())),
        "maximum_cycle_reached": max(cycles) if cycles else 0,
        "invalid_partial_cycle_count": sum(int(r.get("invalid_partial_cycle") or 0) for r in rows),
        "undercoverage_count": sum(int(safe_float(r.get("undercoverage")) or 0) for r in rows),
        "became_open_count": len(open_rows),
        "max_single_open_loss": min(open_mtms) if open_mtms else 0.0,
    }


def check_d0_s2_parity(summary: dict[str, Any]) -> dict[str, Any]:
    checks = {
        "trades": (
            summary.get("trades_started"),
            S2_REFERENCE_TRADES,
            summary.get("trades_started") == S2_REFERENCE_TRADES,
        ),
        "closed": (
            summary.get("trades_closed"),
            S2_REFERENCE_CLOSED,
            summary.get("trades_closed") == S2_REFERENCE_CLOSED,
        ),
        "blockers": (
            summary.get("open_blocker_count"),
            S2_REFERENCE_BLOCKERS,
            summary.get("open_blocker_count") == S2_REFERENCE_BLOCKERS,
        ),
        "series_mtm": (
            summary.get("total_series_mtm_usdt"),
            S2_REFERENCE_SERIES_MTM,
            abs(safe_float(summary.get("total_series_mtm_usdt")) - S2_REFERENCE_SERIES_MTM)
            <= S2_MTM_TOLERANCE,
        ),
        "invalid_partial": (
            summary.get("invalid_partial_cycle_count"),
            0,
            int(summary.get("invalid_partial_cycle_count") or 0) == 0,
        ),
    }
    return {"ok": all(c[2] for c in checks.values()), "checks": checks}


def shared_initial_entry_row(
    *,
    coin: str,
    candles: list[Any],
    long_first: dict[str, Any] | None,
    short_first: dict[str, Any] | None,
) -> dict[str, Any]:
    c0 = candles[0] if candles else None
    mark = float(c0.close) if c0 is not None else None
    ts = _ts(getattr(c0, "timestamp", None)) if c0 is not None else ""
    long_raw = (long_first or {}).get("start_index")
    short_raw = (short_first or {}).get("start_index")
    long_start = int(long_raw) if long_raw is not None else -1
    short_start = int(short_raw) if short_raw is not None else -2
    return {
        "coin": coin,
        "shared_initial_entry_index": 0,
        "shared_initial_entry_timestamp": ts,
        "shared_initial_mark_price": mark,
        "long_initial_entry_index": long_start if long_first else None,
        "short_initial_entry_index": short_start if short_first else None,
        "long_initial_entry_price": safe_float((long_first or {}).get("entry_price")),
        "short_initial_entry_price": safe_float((short_first or {}).get("entry_price")),
        "start_parity_ok": int(
            long_start == 0 and short_start == 0 and long_first is not None and short_first is not None
        ),
        "note": (
            "Forced smoke entry on candle 0 close for both bots; fill prices may differ by "
            "direction/tick/fee. No lookahead."
        ),
    }


def _side_state_arrays(
    rows: list[dict[str, Any]],
    *,
    n_candles: int,
) -> tuple[list[float], list[tuple[float, float, float, float]], list[int | None], list[bool]]:
    """Per-candle: realized_locked_plus_in_trade, position, active_trade_id, blocked."""
    intervals: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: int(r.get("trade_number") or 0)):
        start = int(row.get("start_index") or 0)
        end = int(row.get("end_index") if row.get("end_index") is not None else start)
        fills_by_abs: dict[int, dict[str, float]] = {}
        cum = 0.0
        for fill in row.get("fill_log") or []:
            local = fill.get("candle_index")
            if local is None:
                continue
            abs_idx = start + int(local)
            cum += safe_float(fill.get("confirmed_closed_pnl"), safe_float(fill.get("closed_pnl")))
            fills_by_abs[abs_idx] = {
                "lq": safe_float(fill.get("long_qty_after")),
                "sq": safe_float(fill.get("short_qty_after")),
                "la": safe_float(fill.get("long_avg_after")),
                "sa": safe_float(fill.get("short_avg_after")),
                "cum": cum,
            }
        intervals.append(
            {
                "start": start,
                "end": end,
                "trade_number": int(row.get("trade_number") or 0),
                "is_blocker": bool(row.get("is_blocker")),
                "closed_pnl": safe_float(row.get("closed_pnl_usdt")),
                "fills_by_abs": fills_by_abs,
            }
        )

    def find_active(i: int) -> dict[str, Any] | None:
        for it in intervals:
            if it["start"] <= i <= it["end"]:
                return it
        return None

    realized = [0.0] * n_candles
    positions: list[tuple[float, float, float, float]] = [(0.0, 0.0, 0.0, 0.0)] * n_candles
    active_trade: list[int | None] = [None] * n_candles
    blocked = [False] * n_candles

    # After an open blocker ends, keep blocked flag for remaining candles.
    final_blocker_start: int | None = None
    final_blocker_tn: int | None = None
    for it in intervals:
        if it["is_blocker"]:
            final_blocker_start = it["start"]
            final_blocker_tn = it["trade_number"]

    for i in range(n_candles):
        it = find_active(i)
        prior_closed = sum(
            jt["closed_pnl"]
            for jt in intervals
            if (not jt["is_blocker"] and jt["end"] < i)
        )
        if it is None:
            # include trades that closed on previous candles only
            realized[i] = sum(
                jt["closed_pnl"] for jt in intervals if (not jt["is_blocker"] and jt["end"] <= i)
            )
            if final_blocker_start is not None and i >= final_blocker_start:
                blocked[i] = True
                active_trade[i] = final_blocker_tn
            continue

        active_trade[i] = it["trade_number"]
        if it["is_blocker"]:
            blocked[i] = True

        fill_idxs = [k for k in it["fills_by_abs"] if k <= i]
        if fill_idxs:
            st = it["fills_by_abs"][max(fill_idxs)]
            positions[i] = (st["lq"], st["sq"], st["la"], st["sa"])
            in_trade = st["cum"]
        else:
            positions[i] = (0.0, 0.0, 0.0, 0.0)
            in_trade = 0.0

        if not it["is_blocker"] and it["end"] == i:
            realized[i] = prior_closed + it["closed_pnl"]
            positions[i] = (0.0, 0.0, 0.0, 0.0)
        else:
            realized[i] = prior_closed + in_trade

    return realized, positions, active_trade, blocked


def build_combined_equity_curve(
    *,
    coin: str,
    candles: list[Any],
    long_rows: list[dict[str, Any]],
    short_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    n = len(candles)
    long_realized, long_pos, long_active, long_blocked = _side_state_arrays(long_rows, n_candles=n)
    short_realized, short_pos, short_active, short_blocked = _side_state_arrays(short_rows, n_candles=n)

    rows: list[dict[str, Any]] = []
    peak = float("-inf")
    max_dd = 0.0
    max_dd_idx = 0
    max_dd_ts = ""

    for i in range(n):
        mark = float(candles[i].close)
        ts = _ts(getattr(candles[i], "timestamp", None))
        llq, lsq, lla, lsa = long_pos[i]
        slq, ssq, sla, ssa = short_pos[i]
        _, _, long_u = calculate_unrealized_pnl(llq, lla, lsq, lsa, mark)
        _, _, short_u = calculate_unrealized_pnl(slq, sla, ssq, ssa, mark)
        long_u = float(long_u or 0.0)
        short_u = float(short_u or 0.0)
        long_eq = long_realized[i] + long_u
        short_eq = short_realized[i] + short_u
        combined = long_eq + short_eq
        if combined > peak:
            peak = combined
        dd = peak - combined
        if dd > max_dd:
            max_dd = dd
            max_dd_idx = i
            max_dd_ts = ts
        rows.append(
            {
                "coin": coin,
                "candle_index": i,
                "timestamp": ts,
                "mark_price": mark,
                "long_realized_pnl": long_realized[i],
                "long_open_mtm": long_u,
                "long_total_equity": long_eq,
                "short_realized_pnl": short_realized[i],
                "short_open_mtm": short_u,
                "short_total_equity": short_eq,
                "combined_equity": combined,
                "long_active_trade_id": long_active[i],
                "short_active_trade_id": short_active[i],
                "long_blocked": int(long_blocked[i]),
                "short_blocked": int(short_blocked[i]),
            }
        )

    final = rows[-1] if rows else {}
    summary = {
        "coin": coin,
        "max_combined_drawdown": max_dd,
        "max_combined_drawdown_candle": max_dd_idx,
        "max_combined_drawdown_timestamp": max_dd_ts,
        "final_combined_equity": safe_float(final.get("combined_equity")),
        "final_long_equity": safe_float(final.get("long_total_equity")),
        "final_short_equity": safe_float(final.get("short_total_equity")),
        "margin_competition_simulated": False,
        "margin_note": (
            "Independent books only; no shared margin/liquidation/cross-wallet competition."
        ),
    }
    return rows, summary


def coin_combined_summary(
    *,
    coin: str,
    long_summary: dict[str, Any],
    short_summary: dict[str, Any],
    equity_summary: dict[str, Any],
) -> dict[str, Any]:
    long_total = safe_float(long_summary.get("total_series_mtm_usdt"))
    short_total = safe_float(short_summary.get("total_series_mtm_usdt"))
    long_blockers = int(long_summary.get("open_blocker_count") or 0)
    short_blockers = int(short_summary.get("open_blocker_count") or 0)
    long_open = safe_float(long_summary.get("final_open_mtm_usdt"))
    short_closed = safe_float(short_summary.get("closed_pnl_usdt"))
    short_open = safe_float(short_summary.get("final_open_mtm_usdt"))
    long_closed = safe_float(long_summary.get("closed_pnl_usdt"))
    short_contribution = short_closed + short_open
    return {
        "coin": coin,
        "long_trades": long_summary.get("trades_started"),
        "short_trades": short_summary.get("trades_started"),
        "long_closed_pnl": long_closed,
        "short_closed_pnl": short_closed,
        "long_open_mtm": long_open,
        "short_open_mtm": short_open,
        "combined_closed_pnl": long_closed + short_closed,
        "combined_open_mtm": long_open + short_open,
        "combined_total_result": long_total + short_total,
        "long_blocker_count": long_blockers,
        "short_blocker_count": short_blockers,
        "both_sides_block": int(long_blockers > 0 and short_blockers > 0),
        "only_long_block": int(long_blockers > 0 and short_blockers == 0),
        "only_short_block": int(short_blockers > 0 and long_blockers == 0),
        "no_blocker": int(long_blockers == 0 and short_blockers == 0),
        "short_covers_long_blocker_mtm": int(
            long_blockers > 0 and short_contribution > abs(min(0.0, long_open))
        ),
        "long_covers_short_blocker_mtm": int(
            short_blockers > 0
            and (long_closed + long_open) > abs(min(0.0, short_open))
        ),
        "short_contribution": short_contribution,
        "combined_delta_vs_long_only": short_contribution,
        "max_combined_drawdown": equity_summary.get("max_combined_drawdown"),
        "final_combined_equity": equity_summary.get("final_combined_equity"),
        "long_invalid_partial": long_summary.get("invalid_partial_cycle_count"),
        "short_invalid_partial": short_summary.get("invalid_partial_cycle_count"),
    }


def select_case_study_coins(coin_rows: list[dict[str, Any]]) -> dict[str, str | None]:
    long_block_short_profit = None
    short_block_long_profit = None
    both_or_dd = None
    best_dd = -1.0
    for row in coin_rows:
        coin = str(row.get("coin") or "")
        if (
            long_block_short_profit is None
            and int(row.get("only_long_block") or 0) == 1
            and safe_float(row.get("short_contribution")) > 0
        ):
            long_block_short_profit = coin
        if (
            short_block_long_profit is None
            and int(row.get("only_short_block") or 0) == 1
            and safe_float(row.get("long_closed_pnl")) + safe_float(row.get("long_open_mtm")) > 0
        ):
            short_block_long_profit = coin
        dd = safe_float(row.get("max_combined_drawdown"))
        if int(row.get("both_sides_block") or 0) == 1 and dd >= best_dd:
            both_or_dd = coin
            best_dd = dd
        elif both_or_dd is None and dd > best_dd:
            both_or_dd = coin
            best_dd = dd
    return {
        "long_blocks_short_profits": long_block_short_profit,
        "short_blocks_long_profits": short_block_long_profit,
        "both_block_or_high_dd": both_or_dd,
        "aptusdt": "APTUSDT",
    }


def opener_classification_ok() -> list[dict[str, Any]]:
    cases = [
        ("long", "CYCLE_2_LONG_ADD"),
        ("long", "CYCLE_2_SHORT_REDUCE"),
        ("short", "CYCLE_2_SHORT_REDUCE"),
        ("short", "CYCLE_2_SHORT_ADD"),
        ("short", "CYCLE_2_LONG_REDUCE"),
    ]
    rows = []
    for primary, purpose in cases:
        rows.append(
            {
                "primary_side": primary,
                "purpose": purpose,
                "is_opener": int(is_direction_aware_cycle_opener(purpose, primary_side=primary)),
                "is_second_leg": int(is_direction_aware_second_leg(purpose, primary_side=primary)),
            }
        )
    return rows


def validate_independent_reentry_offsets(rows: list[dict[str, Any]]) -> bool:
    ordered = sorted(rows, key=lambda r: int(r.get("trade_number") or 0))
    for left, right in zip(ordered, ordered[1:]):
        expected = int(left.get("end_index") or 0) + 1
        actual = int(right.get("start_index") or 0)
        if actual != expected:
            return False
        if actual == int(left.get("end_index") or 0):
            return False
    return True

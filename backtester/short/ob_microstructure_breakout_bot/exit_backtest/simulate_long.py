"""Simulate one long trade with pool + OB/public-trade exit rules."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from ob_microstructure_breakout_bot.data.bars import load_5m_bars
from ob_microstructure_breakout_bot.data.orderbook import sample_ob_bands
from ob_microstructure_breakout_bot.data.trades import load_trade_window
from ob_microstructure_breakout_bot.exit_backtest.cluster_mass import (
    pick_calibrated_long_tp_cluster,
)
from ob_microstructure_breakout_bot.exit_backtest.pools import (
    UpperPool,
    load_upper_pools_as_of,
    pick_meaningful_ladder,
    pools_above_price,
)
from ob_microstructure_breakout_bot.exit_backtest.stops import compute_long_sl
from ob_microstructure_breakout_bot.exit_backtest.thresholds import (
    DELTA_OK,
    DELTA_STRONG,
    FIRST_POOL_FEE_DEAD_PCT,
    FIRST_POOL_MIN_DIST_PCT,
    MAX_HOLD_HOURS,
    MIN_MEANINGFUL_POOL_SEP_PCT,
    OB_BID_OK,
    OB_BID_STRONG,
    SECOND_POOL_FAR_PCT,
    SECOND_POOL_VOID_PCT,
    TP_APPROACH_PCT,
    WEAK_DELTA_BUFFER,
)


@dataclass
class LongTradeResult:
    decision_ts: str
    tier: str
    confirm_delta: float
    followthrough_delta: float
    entry_price: float
    sl_price: float
    pivot_low: float
    pivot_ts: str
    first_pool_bottom: float | None
    first_pool_top: float | None
    second_pool_bottom: float | None
    second_pool_top: float | None
    first_pool_dist_pct: float | None
    second_pool_sep_pct: float | None
    tp_cluster_strength_sum: float | None
    tp_cluster_n_pools: int | None
    ignored: bool
    ignore_reason: str | None
    initial_tp: float | None
    final_tp: float | None
    tp_mode: str | None
    entry_ob_ratio: float | None
    pool_touch_ts: str | None
    pool_touch_delta: float | None
    pool_touch_ob_ratio: float | None
    continuation: bool | None
    exit_ts: str | None
    exit_price: float | None
    exit_reason: str
    pnl_pct: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _pct_above(entry: float, level: float) -> float:
    return (level - entry) / entry


def _ob_ratio_at(symbol: str, when: datetime) -> float | None:
    try:
        ob = sample_ob_bands(symbol, when)
    except Exception:
        return None
    if ob.ask_5bps <= 0:
        return None
    return ob.bid_5bps / ob.ask_5bps


def _touch_delta(symbol: str, when: datetime) -> float:
    """Public-trade delta in a 10m window ending at bar close (no lookahead)."""
    end = when + timedelta(minutes=5)
    start = end - timedelta(minutes=10)
    win = load_trade_window(symbol, start, end)
    return float(win.delta_notional)


def _apply_continuation_decision(
    *,
    first_bottom: float,
    second_bottom: float | None,
    second_top: float | None,
    pool_touch_delta: float,
    pool_touch_ob_ratio: float | None,
    void_second: bool,
    far_second: bool,
) -> tuple[bool, float, str]:
    """Return (continuation, final_tp, tp_mode) from approach/touch flow."""
    delta_strong = pool_touch_delta >= DELTA_STRONG
    delta_ok = pool_touch_delta >= DELTA_OK
    ob_strong = (
        pool_touch_ob_ratio is not None and pool_touch_ob_ratio >= OB_BID_STRONG
    )
    ob_ok = pool_touch_ob_ratio is not None and pool_touch_ob_ratio >= OB_BID_OK
    flow_strong = delta_strong and (ob_ok or ob_strong or pool_touch_ob_ratio is None)
    flow_ok = delta_ok and (ob_ok or ob_strong)
    void_lock = void_second and not flow_strong and not flow_ok

    if second_top is not None and not void_lock and flow_strong:
        return True, second_top, "second_pool_upper"
    if second_bottom is not None and not void_lock and flow_ok and not far_second:
        return True, second_bottom, "second_pool_lower"
    if not delta_ok or void_lock:
        return (
            False,
            first_bottom * (1.0 - WEAK_DELTA_BUFFER),
            "first_pool_full_exit_void" if void_lock else "first_pool_weak_buffer",
        )
    return False, first_bottom, "first_pool_lower"


def simulate_long(
    symbol: str,
    *,
    decision_ts: datetime,
    tier: str,
    confirm_delta: float,
    followthrough_delta: float,
) -> LongTradeResult:
    sl_price, pivot_low, pivot_ts = compute_long_sl(symbol, decision_ts)

    bars_entry = load_5m_bars(
        symbol,
        decision_ts - timedelta(minutes=30),
        decision_ts + timedelta(minutes=10),
    )
    entry_bar = None
    for b in bars_entry:
        if b.ts == decision_ts:
            entry_bar = b
            break
    if entry_bar is None and bars_entry:
        prior = [b for b in bars_entry if b.ts < decision_ts]
        entry_bar = prior[-1] if prior else bars_entry[-1]
    if entry_bar is None:
        raise RuntimeError(f"No entry bar at {decision_ts.isoformat()}")
    entry_price = float(entry_bar.open if entry_bar.ts == decision_ts else entry_bar.close)
    entry_ob_ratio = _ob_ratio_at(symbol, decision_ts)

    # 5m ladder for fallback / display
    all_pools = load_upper_pools_as_of(symbol, decision_ts)
    above = pools_above_price(all_pools, entry_price)
    first, second = pick_meaningful_ladder(
        above, min_sep_pct=MIN_MEANINGFUL_POOL_SEP_PCT
    )

    # Phase-C calibrated mass TP from 1m clusters (preferred when flow allows).
    mass = pick_calibrated_long_tp_cluster(
        symbol,
        decision_ts=decision_ts,
        entry_price=entry_price,
        confirm_delta=confirm_delta,
        entry_ob=entry_ob_ratio,
    )
    tp_cluster_strength_sum: float | None = None
    tp_cluster_n_pools: int | None = None
    if mass is not None:
        # Represent mass cluster as the "second" target for reporting.
        second = UpperPool(
            bottom=mass.bottom,
            top=mass.top,
            pool_id=mass.cluster_id,
            status="ACTIVE",
        )
        tp_cluster_strength_sum = float(mass.strength_sum)
        tp_cluster_n_pools = int(mass.n_pools)

    first_dist = _pct_above(entry_price, first.bottom) if first else None
    second_sep = None
    if first and second:
        second_sep = _pct_above(entry_price, second.bottom) - (first_dist or 0.0)

    if first is None and mass is None:
        return LongTradeResult(
            decision_ts=decision_ts.isoformat(),
            tier=tier,
            confirm_delta=confirm_delta,
            followthrough_delta=followthrough_delta,
            entry_price=entry_price,
            sl_price=sl_price,
            pivot_low=pivot_low,
            pivot_ts=pivot_ts,
            first_pool_bottom=None,
            first_pool_top=None,
            second_pool_bottom=None,
            second_pool_top=None,
            first_pool_dist_pct=None,
            second_pool_sep_pct=None,
            tp_cluster_strength_sum=None,
            tp_cluster_n_pools=None,
            ignored=True,
            ignore_reason="no_upper_pool",
            initial_tp=None,
            final_tp=None,
            tp_mode=None,
            entry_ob_ratio=entry_ob_ratio,
            pool_touch_ts=None,
            pool_touch_delta=None,
            pool_touch_ob_ratio=None,
            continuation=None,
            exit_ts=None,
            exit_price=None,
            exit_reason="ignored",
            pnl_pct=None,
        )

    # If only mass cluster exists, synthesize a near checkpoint from its bottom.
    if first is None and mass is not None:
        first = UpperPool(
            bottom=mass.bottom,
            top=mass.top,
            pool_id=mass.cluster_id,
            status="ACTIVE",
        )
        first_dist = _pct_above(entry_price, first.bottom)

    assert first is not None

    far_second = second_sep is not None and second_sep >= SECOND_POOL_FAR_PCT
    void_second = second_sep is not None and second_sep >= SECOND_POOL_VOID_PCT

    # Fee filter — narrow on purpose so stretch winners are not blocked:
    # 1) Void / no second → locked to first; need >= 0.4% room.
    # 2) First pool itself below fees+dust (~0.16%) and no live stretch evidence.
    # Mass TP always bypasses. Stretch needs strong confirm + healthy followthrough.
    if mass is None and first_dist is not None:
        locked_to_first = second is None or void_second
        stretch_ok = (
            second is not None
            and not void_second
            and confirm_delta >= DELTA_STRONG
            and followthrough_delta >= DELTA_OK
        )
        fee_dead = first_dist < FIRST_POOL_FEE_DEAD_PCT and not stretch_ok
        void_thin = locked_to_first and first_dist < FIRST_POOL_MIN_DIST_PCT
        if fee_dead or void_thin:
            return LongTradeResult(
                decision_ts=decision_ts.isoformat(),
                tier=tier,
                confirm_delta=confirm_delta,
                followthrough_delta=followthrough_delta,
                entry_price=entry_price,
                sl_price=sl_price,
                pivot_low=pivot_low,
                pivot_ts=pivot_ts,
                first_pool_bottom=first.bottom,
                first_pool_top=first.top,
                second_pool_bottom=None if second is None else second.bottom,
                second_pool_top=None if second is None else second.top,
                first_pool_dist_pct=first_dist * 100.0,
                second_pool_sep_pct=(
                    second_sep * 100.0 if second_sep is not None else None
                ),
                tp_cluster_strength_sum=None,
                tp_cluster_n_pools=None,
                ignored=True,
                ignore_reason="reward_below_fees",
                initial_tp=None,
                final_tp=None,
                tp_mode=None,
                entry_ob_ratio=entry_ob_ratio,
                pool_touch_ts=None,
                pool_touch_delta=None,
                pool_touch_ob_ratio=None,
                continuation=None,
                exit_ts=None,
                exit_price=None,
                exit_reason="ignored",
                pnl_pct=None,
            )

    if mass is not None:
        # Project TP to heaviest calibrated cluster upper edge at entry.
        initial_tp = mass.top
        final_tp = mass.top
        tp_mode = "calibrated_mass_cluster"
        continuation = True
        decided_at_pool = True
    else:
        initial_tp = first.bottom
        tp_mode = "first_pool_lower"
        if void_second and second is not None and confirm_delta < DELTA_STRONG:
            tp_mode = "first_pool_full_exit_void"
        final_tp = initial_tp
        continuation = None
        decided_at_pool = False

    hold_end = decision_ts + timedelta(hours=MAX_HOLD_HOURS)
    path = load_5m_bars(symbol, decision_ts, hold_end + timedelta(minutes=5))
    path = [b for b in path if b.ts >= decision_ts]
    if not path:
        raise RuntimeError(f"No forward bars after {decision_ts.isoformat()}")

    pool_touch_ts: str | None = None
    pool_touch_delta: float | None = None
    pool_touch_ob_ratio: float | None = None

    for bar in path:
        if bar.low <= sl_price:
            exit_price = sl_price
            pnl = (exit_price - entry_price) / entry_price * 100.0
            return LongTradeResult(
                decision_ts=decision_ts.isoformat(),
                tier=tier,
                confirm_delta=confirm_delta,
                followthrough_delta=followthrough_delta,
                entry_price=entry_price,
                sl_price=sl_price,
                pivot_low=pivot_low,
                pivot_ts=pivot_ts,
                first_pool_bottom=first.bottom,
                first_pool_top=first.top,
                second_pool_bottom=None if second is None else second.bottom,
                second_pool_top=None if second is None else second.top,
                first_pool_dist_pct=first_dist * 100.0 if first_dist is not None else None,
                second_pool_sep_pct=second_sep * 100.0 if second_sep is not None else None,
                tp_cluster_strength_sum=tp_cluster_strength_sum,
                tp_cluster_n_pools=tp_cluster_n_pools,
                ignored=False,
                ignore_reason=None,
                initial_tp=initial_tp,
                final_tp=final_tp,
                tp_mode=tp_mode,
                entry_ob_ratio=entry_ob_ratio,
                pool_touch_ts=pool_touch_ts,
                pool_touch_delta=pool_touch_delta,
                pool_touch_ob_ratio=pool_touch_ob_ratio,
                continuation=continuation,
                exit_ts=bar.ts.isoformat(),
                exit_price=exit_price,
                exit_reason="sl",
                pnl_pct=pnl,
            )

        # Fallback path only: approach-zone continuation when mass TP was not set.
        approach_level = first.bottom * (1.0 - TP_APPROACH_PCT)
        if not decided_at_pool and bar.high >= approach_level:
            pool_touch_delta = _touch_delta(symbol, bar.ts)
            pool_touch_ob_ratio = _ob_ratio_at(symbol, bar.ts)
            pierced = bar.high >= first.bottom
            gap_through = bar.open >= first.bottom
            cont, new_tp, mode = _apply_continuation_decision(
                first_bottom=first.bottom,
                second_bottom=None if second is None else second.bottom,
                second_top=None if second is None else second.top,
                pool_touch_delta=pool_touch_delta,
                pool_touch_ob_ratio=pool_touch_ob_ratio,
                void_second=void_second,
                far_second=far_second,
            )
            if gap_through:
                decided_at_pool = True
                pool_touch_ts = bar.ts.isoformat()
                continuation = False
                final_tp = first.bottom
                tp_mode = "first_pool_late_fill"
            elif cont:
                decided_at_pool = True
                pool_touch_ts = bar.ts.isoformat()
                continuation = True
                final_tp = new_tp
                tp_mode = mode
            elif pierced:
                decided_at_pool = True
                pool_touch_ts = bar.ts.isoformat()
                continuation = False
                final_tp = new_tp
                tp_mode = mode

        if final_tp is not None and bar.high >= final_tp:
            exit_price = final_tp
            pnl = (exit_price - entry_price) / entry_price * 100.0
            return LongTradeResult(
                decision_ts=decision_ts.isoformat(),
                tier=tier,
                confirm_delta=confirm_delta,
                followthrough_delta=followthrough_delta,
                entry_price=entry_price,
                sl_price=sl_price,
                pivot_low=pivot_low,
                pivot_ts=pivot_ts,
                first_pool_bottom=first.bottom,
                first_pool_top=first.top,
                second_pool_bottom=None if second is None else second.bottom,
                second_pool_top=None if second is None else second.top,
                first_pool_dist_pct=first_dist * 100.0 if first_dist is not None else None,
                second_pool_sep_pct=second_sep * 100.0 if second_sep is not None else None,
                tp_cluster_strength_sum=tp_cluster_strength_sum,
                tp_cluster_n_pools=tp_cluster_n_pools,
                ignored=False,
                ignore_reason=None,
                initial_tp=initial_tp,
                final_tp=final_tp,
                tp_mode=tp_mode,
                entry_ob_ratio=entry_ob_ratio,
                pool_touch_ts=pool_touch_ts,
                pool_touch_delta=pool_touch_delta,
                pool_touch_ob_ratio=pool_touch_ob_ratio,
                continuation=continuation,
                exit_ts=bar.ts.isoformat(),
                exit_price=exit_price,
                exit_reason="tp",
                pnl_pct=pnl,
            )

    last = path[-1]
    exit_price = float(last.close)
    pnl = (exit_price - entry_price) / entry_price * 100.0
    return LongTradeResult(
        decision_ts=decision_ts.isoformat(),
        tier=tier,
        confirm_delta=confirm_delta,
        followthrough_delta=followthrough_delta,
        entry_price=entry_price,
        sl_price=sl_price,
        pivot_low=pivot_low,
        pivot_ts=pivot_ts,
        first_pool_bottom=first.bottom,
        first_pool_top=first.top,
        second_pool_bottom=None if second is None else second.bottom,
        second_pool_top=None if second is None else second.top,
        first_pool_dist_pct=first_dist * 100.0 if first_dist is not None else None,
        second_pool_sep_pct=second_sep * 100.0 if second_sep is not None else None,
        tp_cluster_strength_sum=tp_cluster_strength_sum,
        tp_cluster_n_pools=tp_cluster_n_pools,
        ignored=False,
        ignore_reason=None,
        initial_tp=initial_tp,
        final_tp=final_tp,
        tp_mode=tp_mode,
        entry_ob_ratio=entry_ob_ratio,
        pool_touch_ts=pool_touch_ts,
        pool_touch_delta=pool_touch_delta,
        pool_touch_ob_ratio=pool_touch_ob_ratio,
        continuation=continuation,
        exit_ts=last.ts.isoformat(),
        exit_price=exit_price,
        exit_reason="timeout",
        pnl_pct=pnl,
    )

"""Causal unlock / re-lock signal helpers for Emergency-Lock Phase B.

Unlock reference (no lookahead)
-------------------------------
After full lock, ``post_lock_low`` is the lowest ``low`` observed on the lock
bar and all subsequent bars (updated each bar before signals).

For unlock stage ``i``:

    unlock_reference = post_lock_low * (1 + unlock_rebound_pcts[i])

Stage ``i`` fires when ``candle.high >= unlock_reference`` (confirmation
handled by the state machine). Only past and current candle data are used.

Re-lock reference
-----------------
    relock_trigger = last_unlock_fill * (1 - relock_distance_pct)

Fires when ``candle.low <= relock_trigger``.
"""

from __future__ import annotations


def unlock_reference_price(*, post_lock_low: float, rebound_pct: float) -> float:
    return float(post_lock_low) * (1.0 + float(rebound_pct))


def unlock_signal_touched(*, candle_high: float, unlock_reference: float) -> bool:
    return float(candle_high) >= float(unlock_reference)


def relock_trigger_price(*, last_unlock_fill: float, relock_distance_pct: float) -> float:
    return float(last_unlock_fill) * (1.0 - float(relock_distance_pct))


def relock_signal_touched(*, candle_low: float, relock_trigger: float) -> bool:
    return float(candle_low) <= float(relock_trigger)


def tranche_qty_from_full_lock(
    *,
    full_lock_short_qty: float,
    unlock_step_fraction: float,
) -> float:
    """Unlock step fractions always refer to ``full_lock_short_qty``."""
    return float(full_lock_short_qty) * float(unlock_step_fraction)


def net_long_fraction(
    *,
    long_qty: float,
    short_qty: float,
    full_lock_short_qty: float,
) -> float:
    if full_lock_short_qty <= 0.0:
        return 0.0
    net_long = max(float(long_qty) - float(short_qty), 0.0)
    return net_long / float(full_lock_short_qty)


def open_short_profit_usdt(
    *,
    short_qty: float,
    short_avg: float,
    reference_price: float,
) -> float:
    """Open short mark-to-reference profit (positive = short is profitable)."""
    if short_qty <= 0.0 or short_avg <= 0.0:
        return 0.0
    return float(short_qty) * (float(short_avg) - float(reference_price))


def distance_to_short_avg_pct(
    *,
    short_avg: float,
    unlock_fill_reference: float,
) -> float:
    if short_avg <= 0.0:
        return 0.0
    return (float(short_avg) - float(unlock_fill_reference)) / float(short_avg)

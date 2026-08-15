"""Reclaim predicates R1 / R2 / R3."""

from __future__ import annotations

from typing import Any


def reclaim_close(*, side: str, level: float, close: float) -> bool:
    """Close back across level (strict). Exact level close counts as reclaim."""
    if side == "long":
        return close >= level
    return close <= level


def is_bullish_or_above(*, open_: float, close: float, ref: float) -> bool:
    return close > open_ or close > ref


def is_bearish_or_below(*, open_: float, close: float, ref: float) -> bool:
    return close < open_ or close < ref


def r1_same_candle(*, side: str, level: float, swept: bool, close: float) -> bool:
    """Sweep candle trades beyond level and closes back across."""
    return bool(swept) and reclaim_close(side=side, level=level, close=close)


def r2_one_bar(
    *,
    side: str,
    level: float,
    bars_since_sweep: int,
    close: float,
) -> bool:
    """Reclaim on the single bar immediately after the sweep candle."""
    if bars_since_sweep != 1:
        return False
    return reclaim_close(side=side, level=level, close=close)


def r3_confirmation_ok(
    *,
    side: str,
    level: float,
    reclaim_close_px: float,
    open_: float,
    close: float,
) -> bool:
    """One confirmation candle after reclaim."""
    if side == "long":
        if close < level:
            return False
        return is_bullish_or_above(open_=open_, close=close, ref=reclaim_close_px)
    if close > level:
        return False
    return is_bearish_or_below(open_=open_, close=close, ref=reclaim_close_px)


def deeper_break_before_reclaim(
    *,
    side: str,
    level: float,
    prior_extreme: float,
    high: float,
    low: float,
) -> bool:
    """Second deeper/higher break beyond prior sweep extreme before reclaim."""
    if side == "long":
        return low < prior_extreme
    return high > prior_extreme


def reclaim_strength_pct(*, side: str, level: float, close: float) -> float:
    if level == 0:
        return 0.0
    if side == "long":
        return (close - level) / abs(level) * 100.0
    return (level - close) / abs(level) * 100.0


def diagnostic_reclaim_features(
    *,
    side: str,
    level: float,
    open_: float,
    close: float,
    atr: float,
    bars_to_reclaim: int,
) -> dict[str, Any]:
    atr_v = max(atr, 1e-12)
    return {
        "reclaim_strength_pct": reclaim_strength_pct(side=side, level=level, close=close),
        "reclaim_body_atr": abs(close - open_) / atr_v,
        "close_distance_from_level_atr": abs(close - level) / atr_v,
        "bars_to_reclaim": bars_to_reclaim,
    }

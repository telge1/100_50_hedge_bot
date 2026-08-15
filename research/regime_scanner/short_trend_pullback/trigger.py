"""Exhaustion / trigger variants E1–E4 (closed-candle only)."""

from __future__ import annotations

import math
from typing import Any, Mapping

from research.regime_scanner.short_trend_pullback.models import ImpulseState, PullbackState


def _f(row: Mapping[str, Any], *keys: str) -> float | None:
    for k in keys:
        if k not in row:
            continue
        try:
            v = float(row[k])
        except (TypeError, ValueError):
            continue
        if math.isfinite(v):
            return v
    return None


def _candle_metrics(row: Mapping[str, Any]) -> dict[str, float]:
    o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
    rng = max(1e-12, h - l)
    body = abs(c - o)
    upper = h - max(o, c)
    return {
        "bearish": 1.0 if c < o else 0.0,
        "upper_wick_ratio": upper / rng,
        "body_ratio": body / rng,
        "close_location": (c - l) / rng,
    }


def trigger_e1(row: Mapping[str, Any], pb: PullbackState, impulse: ImpulseState) -> bool:
    """Bearish rejection after pullback high formed."""
    if pb.bars < 2:
        return False
    # high already formed earlier (not making new high this bar), or this bar rejects
    m = _candle_metrics(row)
    if m["bearish"] < 1.0:
        return False
    close = float(row["close"])
    # close back under EMA9 or under mid of pullback or under open of prior strength
    e9 = _f(row, "ema_9")
    e20 = _f(row, "ema_20")
    ref = e20 if e20 is not None else e9
    under_ref = ref is not None and close < ref
    under_mid = close < (impulse.end_price + pb.high) / 2.0
    wick_reject = m["upper_wick_ratio"] >= 0.35 or m["body_ratio"] >= 0.45
    # pullback high exists and this bar does not close near high
    near_high = close >= pb.high * 0.998
    return bool(wick_reject and (under_ref or under_mid) and not near_high)


def trigger_e2(row: Mapping[str, Any], pb: PullbackState, impulse: ImpulseState) -> bool:
    """Micro structure flip: bearish micro BOS/CHOCH after pullback activity."""
    if pb.bars < 2:
        return False
    # had some bullish/mixed micro during pullback
    had_bull = pb.internal_bull_bos
    now_bear = bool(row.get("arm_edge_internal_bear")) or bool(row.get("arm_edge_choch_bear"))
    return bool(had_bull and now_bear)


def trigger_e3(row: Mapping[str, Any], pb: PullbackState, impulse: ImpulseState) -> bool:
    """EMA rejection: touched EMA20/59, no sustained close above, then bearish close below."""
    if pb.bars < 2:
        return False
    e20 = _f(row, "ema_20")
    e59 = _f(row, "ema_59")
    close = float(row["close"])
    high = float(row["high"])
    o = float(row["open"])
    # touched band this bar or during pullback (approx via high vs ema)
    touched = False
    level = None
    for ema in (e20, e59):
        if ema is None:
            continue
        if high >= ema:
            touched = True
            level = ema
    if not touched or level is None:
        return False
    # no sustained close above: close below level and bearish
    if close >= level:
        return False
    if close >= o:
        return False
    return True


def trigger_e4(row: Mapping[str, Any], pb: PullbackState, impulse: ImpulseState) -> bool:
    """Pullback low break: after pullback high, break confirmed below local low."""
    if pb.bars < 2:
        return False
    if pb.low_after_high is None or pb.high_bar is None:
        return False
    if pb.low_after_high_bar is None or pb.low_after_high_bar <= pb.high_bar:
        # need a low formed after the high
        if pb.high_bar >= (pb.end_bar or pb.start_bar):
            return False
    close = float(row["close"])
    # confirmed break: close below the post-high swing low
    lvl = float(pb.low_after_high)
    if close >= lvl:
        return False
    # prefer bearish close
    return close < float(row["open"])


def evaluate_trigger(
    trigger: str,
    row: Mapping[str, Any],
    pb: PullbackState,
    impulse: ImpulseState,
) -> bool:
    if trigger == "E1":
        return trigger_e1(row, pb, impulse)
    if trigger == "E2":
        return trigger_e2(row, pb, impulse)
    if trigger == "E3":
        return trigger_e3(row, pb, impulse)
    if trigger == "E4":
        return trigger_e4(row, pb, impulse)
    raise KeyError(trigger)

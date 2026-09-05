"""Paket 2C — proximity watch vs exact touch (Stage A only).

Proximity ≠ touch. Approaching a zone must never start Stage B or emit
directional markers. Exact touch = mid inside the EMA band.
"""

from __future__ import annotations

from typing import Any

# Continuous Discovery V2 research default (not event-tuned).
PROXIMITY_WATCH_MAX_PCT = 0.20


def distance_outside_pct(*, mid: float, dist_outside: float) -> float | None:
    """Outside-band distance as percent of mid price."""
    if mid is None or float(mid) <= 0:
        return None
    return (float(dist_outside) / float(mid)) * 100.0


def is_exact_touch(*, inside_band: bool) -> bool:
    """Exact touch: mid is inside [zone_low, zone_high]."""
    return bool(inside_band)


def is_proximity_watch(
    *,
    inside_band: bool,
    dist_outside: float,
    mid: float,
    max_pct: float = PROXIMITY_WATCH_MAX_PCT,
) -> bool:
    """True when price approaches the band but has not touched it yet."""
    if inside_band:
        return False
    pct = distance_outside_pct(mid=mid, dist_outside=dist_outside)
    if pct is None:
        return False
    return pct <= float(max_pct)


def classify_zone_approach_event(
    *,
    inside_band: bool,
    dist_outside: float,
    mid: float,
    max_pct: float = PROXIMITY_WATCH_MAX_PCT,
) -> dict[str, Any]:
    """Return Stage-A approach classification (never emits direction)."""
    exact = is_exact_touch(inside_band=inside_band)
    prox = is_proximity_watch(
        inside_band=inside_band,
        dist_outside=dist_outside,
        mid=mid,
        max_pct=max_pct,
    )
    pct = distance_outside_pct(mid=mid, dist_outside=dist_outside)
    if exact:
        event = "exact_touch"
    elif prox:
        event = "proximity_watch"
    else:
        event = "none"
    return {
        "zone_event": event,
        "in_proximity": prox,
        "exact_touch": exact,
        "proximity_dist_pct": pct if pct is not None else None,
        "proximity_watch_max_pct": float(max_pct),
        # Stage B / markers only after exact touch (+ later micro confirm).
        "allows_stage_b_from_approach": exact,
        "emit_directional_marker_from_approach": False,
    }


def candle_ohlc_intersects_zone(
    *,
    low: float,
    high: float,
    zone_low: float,
    zone_high: float,
) -> bool:
    """True when the 1m candle range overlaps the EMA band."""
    return float(low) <= float(zone_high) and float(high) >= float(zone_low)


def candle_touch_price_in_zone(
    *,
    low: float,
    high: float,
    close: float,
    zone_low: float,
    zone_high: float,
) -> float | None:
    """Representative touch price inside the band (wick-aware, causal at bar close)."""
    zl, zh = float(zone_low), float(zone_high)
    if not candle_ohlc_intersects_zone(low=low, high=high, zone_low=zl, zone_high=zh):
        return None
    cl, lo, hi = float(close), float(low), float(high)
    if zl <= cl <= zh:
        return cl
    if cl > zh:
        return max(zl, min(lo, zh))
    return max(zl, min(hi, zh))


def classify_zone_approach_from_candle_ohlc(
    *,
    low: float,
    high: float,
    close: float,
    zone_low: float,
    zone_high: float,
    max_pct: float = PROXIMITY_WATCH_MAX_PCT,
) -> dict[str, Any]:
    """Stage-A approach for ema_only: proximity on close, exact touch on OHLC overlap."""
    zl, zh = float(zone_low), float(zone_high)
    cl = float(close)
    exact = candle_ohlc_intersects_zone(low=low, high=high, zone_low=zl, zone_high=zh)
    inside_close = zl <= cl <= zh
    if inside_close:
        dist_outside = 0.0
    elif cl > zh:
        dist_outside = cl - zh
    else:
        dist_outside = zl - cl
    prox = (
        not exact
        and is_proximity_watch(
            inside_band=False,
            dist_outside=dist_outside,
            mid=cl,
            max_pct=max_pct,
        )
    )
    pct = distance_outside_pct(mid=cl, dist_outside=dist_outside)
    touch_price = (
        candle_touch_price_in_zone(
            low=low,
            high=high,
            close=close,
            zone_low=zl,
            zone_high=zh,
        )
        if exact
        else None
    )
    if exact:
        event = "exact_touch"
    elif prox:
        event = "proximity_watch"
    else:
        event = "none"
    return {
        "zone_event": event,
        "in_proximity": prox,
        "exact_touch": exact,
        "proximity_dist_pct": pct if pct is not None else None,
        "proximity_watch_max_pct": float(max_pct),
        "allows_stage_b_from_approach": exact,
        "emit_directional_marker_from_approach": False,
        "touch_price": touch_price,
        "touch_price_basis": "candle_ohlc_1m",
    }

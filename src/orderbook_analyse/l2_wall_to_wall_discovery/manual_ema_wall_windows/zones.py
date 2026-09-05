"""EMA zone bands (fixed methodology, not event-tuned)."""

from __future__ import annotations

from dataclasses import dataclass

from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows import (
    TICK,
    ZONE_ATR_FRAC,
    ZONE_MIN_TICKS,
)


def zone_half_width(atr: float, *, tick: float = TICK) -> float:
    return max(ZONE_ATR_FRAC * atr, ZONE_MIN_TICKS * tick)


@dataclass(frozen=True)
class EmaZone:
    name: str  # EMA20 / EMA59
    center: float
    low: float
    high: float
    half_width: float
    atr: float


def make_zone(name: str, center: float, atr: float) -> EmaZone:
    hw = zone_half_width(atr)
    return EmaZone(name=name, center=center, low=center - hw, high=center + hw, half_width=hw, atr=atr)


def zones_overlap(a: EmaZone, b: EmaZone) -> bool:
    return not (a.high < b.low or b.high < a.low)


def distance_to_zone(price: float, zone: EmaZone) -> dict[str, float]:
    if zone.low <= price <= zone.high:
        inside = 0.0
    elif price < zone.low:
        inside = zone.low - price
    else:
        inside = price - zone.high
    mid = zone.center
    pct = (inside / mid) * 100.0 if mid else 0.0
    ticks = inside / TICK if TICK else 0.0
    atr_mult = inside / zone.atr if zone.atr else 0.0
    return {
        "dist_price": inside,
        "dist_pct": pct,
        "dist_ticks": ticks,
        "dist_atr": atr_mult,
        "inside": 1.0 if inside == 0.0 else 0.0,
    }


def swing_in_zone(swing: float | None, zone: EmaZone) -> bool:
    if swing is None:
        return False
    try:
        s = float(swing)
    except (TypeError, ValueError):
        return False
    return zone.low <= s <= zone.high

"""Level-zone geometry from the original TPO bins. No new VA math."""

from __future__ import annotations

from typing import Any


def tpo_bin_zone(
    *,
    level_price: float,
    price_bin_size: float | None,
    raw_profile: dict[str, Any] | None,
) -> dict[str, Any]:
    """Prefer exact TPO bin edges from the chart payload; else mid ± half step."""
    price = float(level_price)
    bins = ((raw_profile or {}).get("tpo") or {}).get("bins") or []
    for raw in bins:
        mid = raw.get("price_mid")
        low = raw.get("price_low")
        high = raw.get("price_high")
        if mid is None or low is None or high is None:
            continue
        if abs(float(mid) - price) <= 1e-9:
            return {
                "level_price": price,
                "level_zone_low": float(low),
                "level_zone_high": float(high),
                "price_bin_size": float(high) - float(low),
                "zone_source": "TPO_BIN_EDGES",
            }
    if price_bin_size is None:
        raise ValueError("price_bin_size missing and no matching TPO bin")
    step = float(price_bin_size)
    half = step / 2.0
    return {
        "level_price": price,
        "level_zone_low": price - half,
        "level_zone_high": price + half,
        "price_bin_size": step,
        "zone_source": "PRICE_MID_PLUS_MINUS_HALF_BIN",
    }


def intervals_overlap(a_low: float, a_high: float, b_low: float, b_high: float) -> bool:
    return float(a_low) <= float(b_high) and float(b_low) <= float(a_high)


def point_in_zone(price: float, low: float, high: float) -> bool:
    return float(low) <= float(price) <= float(high)


def range_overlaps_zone(range_low: float, range_high: float, zone_low: float, zone_high: float) -> bool:
    return intervals_overlap(range_low, range_high, zone_low, zone_high)

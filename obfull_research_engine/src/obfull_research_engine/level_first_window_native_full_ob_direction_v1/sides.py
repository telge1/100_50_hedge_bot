"""Normalized FRONT/BACK sides. One code path for bullish and bearish."""

from __future__ import annotations

from typing import Any

FRONT_SIDE = "FRONT_SIDE"
BACK_SIDE = "BACK_SIDE"
SUPPORTING_SIDE = "SUPPORTING_SIDE"
OPPOSING_SIDE = "OPPOSING_SIDE"

ACCEPTANCE = {"ACCEPTED_ABOVE", "ACCEPTED_BELOW"}
RECLAIM = {"RECLAIMED_UP", "RECLAIMED_DOWN"}
REJECTION = {
    "REJECTED_UP_FROM_SUPPORT_CONTEXT",
    "REJECTED_DOWN_FROM_RESISTANCE_CONTEXT",
}


def reaction_family(reaction_class: str) -> str:
    if reaction_class in ACCEPTANCE:
        return "ACCEPTANCE"
    if reaction_class in RECLAIM:
        return "RECLAIM"
    if reaction_class in REJECTION:
        return "REJECTION"
    return "UNKNOWN"


def normalized_sides(reaction_direction: str) -> dict[str, str]:
    if reaction_direction == "BULLISH":
        return {
            FRONT_SIDE: "ask",
            BACK_SIDE: "bid",
            SUPPORTING_SIDE: "bid",
            OPPOSING_SIDE: "ask",
        }
    if reaction_direction == "BEARISH":
        return {
            FRONT_SIDE: "bid",
            BACK_SIDE: "ask",
            SUPPORTING_SIDE: "ask",
            OPPOSING_SIDE: "bid",
        }
    raise ValueError("reaction_direction required")


def imbalance_supporting_sign(reaction_direction: str) -> int:
    """+1 = bid-heavier supports bullish; -1 = ask-heavier supports bearish."""
    sides = normalized_sides(reaction_direction)
    return 1 if sides[SUPPORTING_SIDE] == "bid" else -1


def spatial_bands(
    *,
    reaction_class: str,
    reaction_direction: str,
    zone_low: float,
    zone_high: float,
    adjacent_bps: float,
    mid_hint: float | None = None,
) -> dict[str, Any]:
    """Price ranges for level / front / back. Mid only scales the adjacent band."""
    family = reaction_family(reaction_class)
    mid = mid_hint if mid_hint and mid_hint > 0 else (zone_low + zone_high) / 2.0
    adj = mid * (adjacent_bps / 1e4)
    if reaction_direction == "BULLISH":
        beyond_front = (zone_high, zone_high + max(adj, 1e-9) * 50)
        immediately_front = (zone_high, zone_high + adj)
        immediately_back = (zone_low - adj, zone_low)
        behind = (zone_low - max(adj, 1e-9) * 50, zone_low)
    else:
        beyond_front = (zone_low - max(adj, 1e-9) * 50, zone_low)
        immediately_front = (zone_low - adj, zone_low)
        immediately_back = (zone_high, zone_high + adj)
        behind = (zone_high, zone_high + max(adj, 1e-9) * 50)
    zone = (zone_low, zone_high)
    if family == "ACCEPTANCE":
        front = beyond_front
        back = (min(zone[0], behind[0]), max(zone[1], behind[1]))
    elif family == "RECLAIM":
        front = beyond_front
        back = zone
    elif family == "REJECTION":
        front = immediately_front
        back = (min(zone[0], immediately_back[0]), max(zone[1], immediately_back[1]))
    else:
        front = beyond_front
        back = zone
    return {
        "reaction_family": family,
        "level_zone": zone,
        "front_zone": _ordered(front),
        "back_zone": _ordered(back),
        "immediately_above": (zone_high, zone_high + adj),
        "immediately_below": (zone_low - adj, zone_low),
        "adjacent_bps": adjacent_bps,
    }


def price_in(band: tuple[float, float], price: float) -> bool:
    lo, hi = _ordered(band)
    return lo <= price <= hi


def _ordered(band: tuple[float, float]) -> tuple[float, float]:
    return (min(band), max(band))

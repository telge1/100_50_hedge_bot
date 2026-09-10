"""Per-timestamp causal price-response metrics (feature layer only)."""

from __future__ import annotations

from typing import Any

from . import EPSILON, TICK_SIZE
from .microprice import (
    attack_direction,
    midprice,
    microprice,
    on_defender_side,
    side_crossed,
    signed_distance_ticks,
)


def spread_ticks(best_bid: float, best_ask: float, *, tick_size: float = TICK_SIZE) -> float:
    return (float(best_ask) - float(best_bid)) / float(tick_size)


def spread_bps(best_bid: float, best_ask: float) -> float | None:
    mid = midprice(best_bid, best_ask)
    if mid <= EPSILON:
        return None
    return (float(best_ask) - float(best_bid)) / mid * 10000.0


def distance_to_wall_ticks(
    price: float | None,
    *,
    wall_price: float,
    wall_side: str,
    tick_size: float = TICK_SIZE,
) -> float | None:
    """Signed: positive means still on defender side of the wall."""
    if price is None:
        return None
    d = attack_direction(wall_side)
    # Defender-positive distance.
    return (-d) * (float(price) - float(wall_price)) / float(tick_size)


def distance_to_zone_ticks(
    price: float | None,
    *,
    zone_low: float,
    zone_high: float,
    tick_size: float = TICK_SIZE,
) -> float | None:
    """0 inside zone; negative below zone; positive above zone (mid vs zone)."""
    if price is None:
        return None
    p = float(price)
    if p < float(zone_low):
        return (p - float(zone_low)) / float(tick_size)
    if p > float(zone_high):
        return (p - float(zone_high)) / float(tick_size)
    return 0.0


def zone_boundary_crossed(
    price: float | None,
    *,
    zone_low: float,
    zone_high: float,
    wall_side: str,
) -> bool | None:
    """Ask-wall attack crosses above zone_high; bid-wall attack crosses below zone_low."""
    if price is None:
        return None
    d = attack_direction(wall_side)
    if d > 0:
        return float(price) > float(zone_high)
    return float(price) < float(zone_low)


def consumed_price_levels(
    *,
    best_bid: float,
    best_ask: float,
    wall_price: float,
    wall_side: str,
    tick_size: float = TICK_SIZE,
) -> float:
    """Levels beyond the wall in the attack direction (0 if BBO not past wall)."""
    d = attack_direction(wall_side)
    if d > 0:
        # Ask wall: how far best_bid has pushed through/above wall.
        return max(0.0, (float(best_bid) - float(wall_price)) / float(tick_size))
    return max(0.0, (float(wall_price) - float(best_ask)) / float(tick_size))


def compute_point_metrics(
    *,
    best_bid: float,
    best_ask: float,
    best_bid_size: float,
    best_ask_size: float,
    wall_price: float,
    wall_side: str,
    zone_low: float,
    zone_high: float,
    mid_at_anchor: float | None,
    tick_size: float = TICK_SIZE,
) -> dict[str, Any]:
    mid = midprice(best_bid, best_ask)
    mp = microprice(best_bid, best_bid_size, best_ask, best_ask_size)
    d = attack_direction(wall_side)
    attack_from_anchor_ticks = None
    attack_from_anchor_bps = None
    if mid_at_anchor is not None and mid_at_anchor > EPSILON:
        attack_from_anchor_ticks = signed_distance_ticks(
            mid, reference=mid_at_anchor, tick_size=tick_size, wall_side=wall_side
        )
        attack_from_anchor_bps = d * (mid - mid_at_anchor) / mid_at_anchor * 10000.0
    defender_ticks = distance_to_wall_ticks(mid, wall_price=wall_price, wall_side=wall_side, tick_size=tick_size)
    return {
        "best_bid": float(best_bid),
        "best_ask": float(best_ask),
        "best_bid_size": float(best_bid_size),
        "best_ask_size": float(best_ask_size),
        "midprice": mid,
        "microprice": mp,
        "spread_ticks": spread_ticks(best_bid, best_ask, tick_size=tick_size),
        "spread_bps": spread_bps(best_bid, best_ask),
        "distance_to_wall_ticks": distance_to_wall_ticks(
            mid, wall_price=wall_price, wall_side=wall_side, tick_size=tick_size
        ),
        "distance_to_zone_ticks": distance_to_zone_ticks(
            mid, zone_low=zone_low, zone_high=zone_high, tick_size=tick_size
        ),
        "price_progress_attack_ticks": max(0.0, attack_from_anchor_ticks or 0.0)
        if attack_from_anchor_ticks is not None
        else None,
        "price_progress_attack_bps": max(0.0, attack_from_anchor_bps or 0.0)
        if attack_from_anchor_bps is not None
        else None,
        "price_progress_defender_ticks": max(0.0, defender_ticks or 0.0)
        if defender_ticks is not None
        else None,
        "microprice_distance_to_wall_ticks": distance_to_wall_ticks(
            mp, wall_price=wall_price, wall_side=wall_side, tick_size=tick_size
        ),
        "microprice_distance_to_zone_ticks": distance_to_zone_ticks(
            mp, zone_low=zone_low, zone_high=zone_high, tick_size=tick_size
        ),
        "wall_side_crossed": side_crossed(mid, wall_price=wall_price, wall_side=wall_side),
        "zone_boundary_crossed": zone_boundary_crossed(
            mid, zone_low=zone_low, zone_high=zone_high, wall_side=wall_side
        ),
        "price_on_defender_side": on_defender_side(mid, wall_price=wall_price, wall_side=wall_side),
        "microprice_on_defender_side": on_defender_side(mp, wall_price=wall_price, wall_side=wall_side),
        "consumed_price_levels": consumed_price_levels(
            best_bid=best_bid,
            best_ask=best_ask,
            wall_price=wall_price,
            wall_side=wall_side,
            tick_size=tick_size,
        ),
        "microprice_side_crossed": side_crossed(mp, wall_price=wall_price, wall_side=wall_side),
    }

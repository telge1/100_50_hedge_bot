"""USER_PENDING_LIMIT_V1 — sell/buy limit at pool edge until fill or pool deactivation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pool_zone_tracker import (
    LifecycleIndex,
    PoolAnchor,
    anchor_from_signal,
    depth2_matches_at_bar,
    disappeared_matches_at_bar,
    matches_anchor,
)


@dataclass
class PendingFillResult:
    status: str
    fill_utc: str | None
    fill_price: float | None
    cancel_utc: str | None
    rule_id: str
    note: str
    bars_waited: int


def _bar_touch(limit: float, bar: dict) -> bool:
    return bar["l"] <= limit <= bar["h"]


def _density_lost_at_bar(anchor: PoolAnchor, bar_utc: str, lifecycle: LifecycleIndex) -> bool:
    for e in lifecycle.events_at(bar_utc):
        if e.get("event_type") != "POOL_DENSITY_DECREASED":
            continue
        if not matches_anchor(anchor, e):
            continue
        if int(float(e.get("depth") or 0)) < 2:
            return True
    return False


def simulate_user_pending_limit(
    direction: str,
    limit_price: float,
    pool_lower: float,
    pool_upper: float,
    pool_identity: str,
    order_active_from_utc: str,
    prices: dict[str, dict],
    pilot_to: str,
    lifecycle: LifecycleIndex,
) -> PendingFillResult:
    anchor = anchor_from_signal(direction, pool_lower, pool_upper, pool_identity)
    times = sorted(prices.keys())
    bars_waited = 0
    zone_active: bool | None = None
    for t in times:
        if t < order_active_from_utc:
            continue
        if t > pilot_to:
            break
        bars_waited += 1
        matches = depth2_matches_at_bar(anchor, t, lifecycle)
        disappeared = disappeared_matches_at_bar(anchor, t, lifecycle)
        amb = [m for m in matches if m.get("identity_confidence") == "NOT_VERIFIED"]
        if len(amb) > 1:
            return PendingFillResult(
                "POOL_IDENTITY_AMBIGUOUS",
                None,
                None,
                None,
                "EXEC-POOL-IDENTITY-01",
                "Multiple NOT_VERIFIED depth-2 matches",
                bars_waited,
            )

        if matches:
            zone_active = True
        elif disappeared or _density_lost_at_bar(anchor, t, lifecycle):
            zone_active = False

        touch = _bar_touch(limit_price, prices[t])

        if touch and disappeared and not matches:
            return PendingFillResult(
                "AMBIGUOUS_POOL_FILL_SEQUENCE",
                None,
                None,
                None,
                "EXEC-AMBIG-01",
                "Limit touch and POOL_DISAPPEARED same bar; order not provable",
                bars_waited,
            )
        if touch and zone_active:
            return PendingFillResult(
                "ORDER_FILLED",
                t,
                limit_price,
                None,
                "EXEC-USER-PENDING-LIMIT-V1",
                "Limit fill at pool level while depth-2 zone active",
                bars_waited,
            )
        if zone_active is False and not touch:
            return PendingFillResult(
                "ORDER_CANCELLED_POOL_DEACTIVATED",
                None,
                None,
                t,
                "EXEC-POOL-DEACTIVATED-01",
                "Entry depth-2 zone lost before fill",
                bars_waited,
            )
        if zone_active is False and touch:
            return PendingFillResult(
                "ORDER_CANCELLED_POOL_DEACTIVATED",
                None,
                None,
                t,
                "EXEC-POOL-DEACTIVATED-01",
                "Touch after zone deactivation (no fill)",
                bars_waited,
            )
    if zone_active:
        return PendingFillResult(
            "ORDER_PENDING_AT_DATA_END",
            None,
            None,
            None,
            "EXEC-USER-PENDING-LIMIT-V1",
            "Zone still active; limit not touched before pilot end",
            bars_waited,
        )
    return PendingFillResult(
        "ORDER_CANCELLED_POOL_DEACTIVATED",
        None,
        None,
        None,
        "EXEC-POOL-DEACTIVATED-01",
        "Zone inactive before pilot end without fill",
        bars_waited,
    )

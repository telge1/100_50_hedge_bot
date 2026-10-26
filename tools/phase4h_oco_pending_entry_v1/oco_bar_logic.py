"""Per-bar zone and touch evaluation for one pending order (shared with 4E semantics)."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

TOOLS4D = Path(__file__).resolve().parents[1] / "phase4d_pool_landscape_backtester_v2"
sys.path.insert(0, str(TOOLS4D))

from pool_zone_tracker import (
    LifecycleIndex,
    PoolAnchor,
    depth2_matches_at_bar,
    disappeared_matches_at_bar,
    matches_anchor,
)


@dataclass
class BarOrderState:
    matches: bool
    disappeared: bool
    zone_active: bool | None
    touch: bool
    identity_ambiguous: bool
    ambiguous_touch_disappear: bool


def bar_touch(limit: float, bar: dict) -> bool:
    return bar["l"] <= limit <= bar["h"]


def density_lost_at_bar(anchor: PoolAnchor, bar_utc: str, lifecycle: LifecycleIndex) -> bool:
    for e in lifecycle.events_at(bar_utc):
        if e.get("event_type") != "POOL_DENSITY_DECREASED":
            continue
        if not matches_anchor(anchor, e):
            continue
        if int(float(e.get("depth") or 0)) < 2:
            return True
    return False


def eval_bar(
    anchor: PoolAnchor,
    limit: float,
    bar_utc: str,
    bar: dict,
    lifecycle: LifecycleIndex,
    zone_active: bool | None,
) -> tuple[BarOrderState, bool | None]:
    """Return bar state and updated zone_active."""
    matches = depth2_matches_at_bar(anchor, bar_utc, lifecycle)
    disappeared = bool(disappeared_matches_at_bar(anchor, bar_utc, lifecycle))
    amb = [m for m in matches if m.get("identity_confidence") == "NOT_VERIFIED"]
    identity_ambiguous = len(amb) > 1
    if matches:
        zone_active = True
    elif disappeared or density_lost_at_bar(anchor, bar_utc, lifecycle):
        zone_active = False
    touch = bar_touch(limit, bar)
    ambiguous_touch_disappear = touch and disappeared and not matches
    return (
        BarOrderState(
            matches=bool(matches),
            disappeared=disappeared,
            zone_active=zone_active,
            touch=touch,
            identity_ambiguous=identity_ambiguous,
            ambiguous_touch_disappear=ambiguous_touch_disappear,
        ),
        zone_active,
    )

"""Incremental consecutive ACTIVE streak per exact lower atomic band key."""

from __future__ import annotations

from typing import Any

from lower_atomic_pool_key_v1 import canonical_lower_atomic_pool_key
from pool_persistence_v2d_v1 import active_lower_pool_keys


def update_consecutive_lower_streaks(
    snapshot: dict[str, Any],
    streak: dict[str, int],
    *,
    min_unique_origins: int = 2,
) -> dict[str, int]:
    active = active_lower_pool_keys(snapshot, min_unique_origins=min_unique_origins)
    universe = set(streak.keys()) | active
    for pk in universe:
        streak[pk] = streak.get(pk, 0) + 1 if pk in active else 0
    return streak


def streak_for_band(streak: dict[str, int], pool_lo: float, pool_hi: float) -> int:
    return streak.get(canonical_lower_atomic_pool_key(pool_lo, pool_hi), 0)

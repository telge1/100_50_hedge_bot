"""Consecutive ACTIVE bar streaks per atomic lower pool (read-only bundle scan)."""

from __future__ import annotations

from typing import Any

from lower_atomic_pool_key_v1 import canonical_lower_atomic_pool_key


def active_lower_pool_keys(snapshot: dict[str, Any], *, min_unique_origins: int = 2) -> set[str]:
    keys: set[str] = set()
    for b in snapshot.get("lower_bands") or []:
        if str(b.get("pool_status") or "").upper() != "ACTIVE":
            continue
        if int(b.get("unique_origin_count") or 0) < min_unique_origins:
            continue
        lo, hi = float(b["lower_price"]), float(b["upper_price"])
        keys.add(canonical_lower_atomic_pool_key(lo, hi))
    return keys


def streak_at_close(
    session,
    closes: list[str],
    close_index: int,
    *,
    min_unique_origins: int = 2,
) -> dict[str, int]:
    """Streak length ending at closes[close_index] for each atomic lower pool key."""
    streak: dict[str, int] = {}
    for i in range(close_index + 1):
        bar = closes[i]
        snap = session.collect_at_close(bar)
        active = active_lower_pool_keys(snap, min_unique_origins=min_unique_origins)
        seen = set(streak.keys()) | active
        for pk in seen:
            if pk in active:
                streak[pk] = streak.get(pk, 0) + 1
            else:
                streak[pk] = 0
    return streak

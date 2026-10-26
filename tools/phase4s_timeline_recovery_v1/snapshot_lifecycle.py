"""Pool zone state from causal per-bar snapshots (Phase 4S)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ZoneStatus:
    state: str
    rule_id: str
    note: str


def find_pool_track(
    snap: dict[str, Any],
    side: str,
    pool_lower: float,
    pool_upper: float,
    pool_identity: str,
) -> str | None:
    key = "upper_pools" if side == "upper" else "lower_pools"
    origins = set(pool_identity.split("|")) if pool_identity else set()
    best = None
    for p in snap.get(key) or []:
        if int(p.get("depth") or 0) < 2:
            continue
        if abs(float(p["bottom_price"]) - pool_lower) > 1e-8:
            continue
        if abs(float(p["top_price"]) - pool_upper) > 1e-8:
            continue
        if origins and not (set(p.get("origins") or []) & origins):
            continue
        best = p.get("pool_track_id")
    return best


def zone_status_from_snapshot(
    snap_index: dict[str, dict],
    bar_utc: str,
    side: str,
    pool_lower: float,
    pool_upper: float,
    pool_identity: str,
) -> ZoneStatus:
    snap = snap_index.get(bar_utc)
    if not snap:
        return ZoneStatus("INACTIVE", "SNAP-MISSING", "no snapshot at bar")
    track = find_pool_track(snap, side, pool_lower, pool_upper, pool_identity)
    if not track:
        return ZoneStatus("INACTIVE", "SNAP-POOL-ABSENT", "bounds/origins not in depth-2 set")
    return ZoneStatus("ACTIVE", "SNAP-DEPTH2-01", track)

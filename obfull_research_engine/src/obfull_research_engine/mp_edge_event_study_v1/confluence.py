"""Same-role confluence clustering for previous_closed MP edges.

Deterministic tie-break rule (documented):
1. Separate UPPER and LOWER — never mix roles.
2. Sort candidate levels by (price ascending, timeframe order 30m→1h→4h, level_id).
3. Greedy left-to-right adjacent merge: a level joins the current open cluster iff
   bps distance to the cluster's running high (for price-sorted scan) is
   ≤ confluence_tolerance_bps; otherwise start a new cluster.
4. Each level is assigned to exactly one cluster (no multi-zone membership).
5. Cluster class is derived from the frozenset of member timeframes.
6. zone_id = hash(role, class, sorted(tf:price_rounded), asof bucket optional).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Sequence

from .schema import (
    CONFLUENCE_CLASS_BY_TFS,
    TF_ORDER,
    ActiveLevel,
    ConfluenceZone,
)
from .util import bps_distance, stable_hash


def confluence_class_for(timeframes: Sequence[str]) -> str:
    key = frozenset(timeframes)
    if key not in CONFLUENCE_CLASS_BY_TFS:
        raise ValueError(f"unsupported timeframe set for confluence: {sorted(key)}")
    return CONFLUENCE_CLASS_BY_TFS[key]


def _zone_id(role: str, cls: str, members: Sequence[ActiveLevel]) -> str:
    parts = [role, cls] + [f"{m.timeframe}:{round(m.level_price, 4)}" for m in members]
    return "zn_" + stable_hash(parts, n=16)


def cluster_same_role(
    levels: Sequence[ActiveLevel],
    *,
    role: str,
    confluence_tolerance_bps: float,
    asof_ns: int,
) -> list[ConfluenceZone]:
    """Cluster equal-role active levels. Empty input → []."""
    group = [lv for lv in levels if lv.role == role]
    if not group:
        return []
    group = sorted(
        group,
        key=lambda m: (m.level_price, TF_ORDER.get(m.timeframe, 99), m.level_id),
    )
    clusters: list[list[ActiveLevel]] = []
    current: list[ActiveLevel] = [group[0]]
    for lv in group[1:]:
        # distance to current cluster high (adjacent in price-sorted order)
        ref = max(m.level_price for m in current)
        if bps_distance(lv.level_price, ref) <= float(confluence_tolerance_bps):
            current.append(lv)
        else:
            clusters.append(current)
            current = [lv]
    clusters.append(current)

    zones: list[ConfluenceZone] = []
    for members in clusters:
        members = sorted(
            members,
            key=lambda m: (TF_ORDER.get(m.timeframe, 99), m.level_id),
        )
        prices = [m.level_price for m in members]
        lo = min(prices)
        hi = max(prices)
        center = (lo + hi) / 2.0
        width = bps_distance(hi, lo) if hi != lo else 0.0
        tfs = sorted({m.timeframe for m in members}, key=lambda t: TF_ORDER.get(t, 99))
        cls = confluence_class_for(tfs)
        zones.append(
            ConfluenceZone(
                zone_id=_zone_id(role, cls, members),
                role=role,
                confluence_class=cls,
                confluence_low=float(lo),
                confluence_high=float(hi),
                confluence_center=float(center),
                confluence_width_bps=float(width),
                timeframes=tfs,
                level_ids=[m.level_id for m in members],
                profile_ids=[m.profile_id for m in members],
                levels=prices,
                asof_ns=int(asof_ns),
            )
        )
    return zones


def build_active_confluence(
    active_levels: Sequence[ActiveLevel],
    *,
    confluence_tolerance_bps: float,
    asof_ns: int,
) -> list[ConfluenceZone]:
    upper = cluster_same_role(
        active_levels,
        role="UPPER",
        confluence_tolerance_bps=confluence_tolerance_bps,
        asof_ns=asof_ns,
    )
    lower = cluster_same_role(
        active_levels,
        role="LOWER",
        confluence_tolerance_bps=confluence_tolerance_bps,
        asof_ns=asof_ns,
    )
    # Deterministic global order: role, center, zone_id
    out = upper + lower
    out.sort(key=lambda z: (0 if z.role == "UPPER" else 1, z.confluence_center, z.zone_id))
    return out


def zone_to_row(z: ConfluenceZone) -> dict:
    return asdict(z)

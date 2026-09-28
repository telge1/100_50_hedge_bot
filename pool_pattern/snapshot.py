"""One causal cluster picture at a 4h close.

Intensity is how many bands cover the same price. Volume strength is not used.
A single pool at least 3 % from the next pool stays relevant.
A narrow band sitting in the gap in front of a heavier zone does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pool_pattern.market import ensure_paths
from pool_pattern.profile import PatternProfile


@dataclass(frozen=True)
class Zone:
    side: str
    low: float
    high: float
    pool_count: int
    intensity: int
    width_pct: float
    born: datetime
    heavy: bool
    relevant: bool
    thin: bool
    pool_ids: tuple[str, ...]


def _gap_pct(near: float, far: float) -> float:
    if near <= 0 or far <= near:
        return 0.0
    return (far - near) / near * 100.0


def overlap_intensity(pools) -> int:
    events: list[tuple[float, int, int]] = []
    for pool in pools:
        events.append((float(pool.bottom_price), 0, 1))
        events.append((float(pool.top_price), 1, -1))
    events.sort()
    current = 0
    best = 0
    for _price, _order, delta in events:
        current += delta
        if current > best:
            best = current
    return best


def zones_at(pools, as_of: datetime, profile: PatternProfile) -> list[Zone]:
    ensure_paths()
    from indicators.liquidity_location.clusters import cluster_pools, filter_clusters

    clusters = cluster_pools(pools, gap_pct=profile.cluster_gap_pct, as_of=as_of, active_only=False)
    stacks = {cluster.cluster_id for cluster in filter_clusters(clusters, minimum_pools=3)}
    built: list[Zone] = []
    for cluster in clusters:
        intensity = overlap_intensity(cluster.pools)
        mid = cluster.cluster_mid or cluster.cluster_low
        width_pct = 0.0 if mid <= 0 else (cluster.cluster_high - cluster.cluster_low) / mid * 100.0
        born = min(pool.created_timestamp for pool in cluster.pools)
        heavy = cluster.cluster_id in stacks or intensity >= 2
        narrow = (not heavy) and width_pct < profile.relevant_pool_width_pct
        built.append(
            Zone(
                side=cluster.side,
                low=float(cluster.cluster_low),
                high=float(cluster.cluster_high),
                pool_count=cluster.pool_count,
                intensity=intensity,
                width_pct=width_pct,
                born=born,
                heavy=heavy,
                relevant=heavy,
                thin=narrow,
                pool_ids=tuple(cluster.pool_ids),
            )
        )
    return _mark_thin_gaps(built, profile)


def _mark_thin_gaps(zones: list[Zone], profile: PatternProfile) -> list[Zone]:
    marked: list[Zone] = []
    for side in ("upper", "lower"):
        group = sorted((zone for zone in zones if zone.side == side), key=lambda zone: (zone.low, zone.high))
        for index, zone in enumerate(group):
            if zone.heavy or not zone.thin:
                marked.append(zone if zone.relevant else _copy(zone, relevant=True, thin=False))
                continue
            later = [
                other
                for other in group[index + 1 :]
                if other.heavy or other.width_pct >= profile.relevant_pool_width_pct
            ]
            earlier = [
                other
                for other in group[:index]
                if other.heavy or other.width_pct >= profile.relevant_pool_width_pct
            ]
            if later and earlier:
                outer = _gap_pct(earlier[-1].high, later[0].low)
                if outer >= profile.cluster_gap_pct:
                    marked.append(_copy(zone, relevant=False, thin=True))
                    continue
            marked.append(_copy(zone, relevant=True, thin=False))
    return marked


def _copy(zone: Zone, *, relevant: bool, thin: bool) -> Zone:
    return Zone(
        zone.side,
        zone.low,
        zone.high,
        zone.pool_count,
        zone.intensity,
        zone.width_pct,
        zone.born,
        zone.heavy,
        relevant,
        thin,
        zone.pool_ids,
    )


def relevant(zones: list[Zone], side: str) -> list[Zone]:
    return sorted((zone for zone in zones if zone.side == side and zone.relevant), key=lambda zone: zone.low)


def zone_touched(zone: Zone, high: float, low: float, close: float) -> bool:
    return zone.low <= high and low <= zone.high and (zone.low <= close or zone.low <= high)


def next_relevant_above(zones: list[Zone], level: float) -> tuple[Zone | None, float]:
    """Next relevant upper above level. Thin in-gap bands are already marked not relevant."""
    above = [zone for zone in relevant(zones, "upper") if zone.low > level]
    if not above:
        return None, 0.0
    zone = above[0]
    return zone, _gap_pct(level, zone.low)

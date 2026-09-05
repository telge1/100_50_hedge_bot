"""Individual vs cluster entry-pool selection (V2 contract)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .config import APPROACH_ATR_MULT, ENTRY_FRACTION_FROM_LOWER
from .models import PoolRecord


def pools_overlap(a: PoolRecord, b: PoolRecord) -> bool:
    if a.side != b.side:
        return False
    lo = max(a.lower_edge, b.lower_edge)
    hi = min(a.upper_edge, b.upper_edge)
    if hi <= lo:
        return False
    overlap = hi - lo
    min_width = min(a.upper_edge - a.lower_edge, b.upper_edge - b.lower_edge)
    return overlap >= 0.35 * min_width


def _distance_atr(price: float, pool: PoolRecord, atr: float) -> float:
    if not atr or atr != atr or atr <= 0:
        return float("inf")
    edge = pool.near_edge
    return abs(price - edge) / atr


def pullback_limit_price(pool: PoolRecord, *, direction: str) -> float:
    width = pool.upper_edge - pool.lower_edge
    if direction == "SHORT":
        return pool.lower_edge + ENTRY_FRACTION_FROM_LOWER * width
    return pool.upper_edge - ENTRY_FRACTION_FROM_LOWER * width


def _pool_reachability(price: float, pool: PoolRecord, *, direction: str, atr: float) -> bool:
    limit_px = pullback_limit_price(pool, direction=direction)
    if _distance_atr(price, pool, atr) > APPROACH_ATR_MULT:
        return False
    if direction == "SHORT":
        return price <= pool.upper_edge + (atr or 0) * 0.5
    return price >= pool.lower_edge - (atr or 0) * 0.5


def _selection_score(price: float, pool: PoolRecord, *, direction: str) -> tuple[float, float, int]:
    """Lower is better: distance, prefer individual (0), prefer narrower overlap."""
    dist = abs(price - pullback_limit_price(pool, direction=direction))
    is_cluster = 0 if pool.component_count == 1 and str(pool.pool_id).startswith("lld:") else 1
    width = pool.upper_edge - pool.lower_edge
    return (dist, is_cluster, width)


def _pick_representative(group: list[PoolRecord], *, price: float, direction: str) -> tuple[PoolRecord, str]:
    individuals = [p for p in group if p.component_count == 1 and str(p.pool_id).startswith("lld:")]
    if individuals:
        chosen = min(individuals, key=lambda p: _selection_score(price, p, direction=direction))
        if len(group) > 1:
            return chosen, "individual_preferred_in_overlap_group"
        return chosen, "individual_pool_nearest_limit"
    chosen = min(group, key=lambda p: _selection_score(price, p, direction=direction))
    return chosen, "cluster_only_in_zone"


def group_overlapping_pools(pools: list[PoolRecord]) -> list[list[PoolRecord]]:
    if not pools:
        return []
    remaining = list(pools)
    groups: list[list[PoolRecord]] = []
    while remaining:
        seed = remaining.pop(0)
        group = [seed]
        i = 0
        while i < len(remaining):
            if any(pools_overlap(seed, remaining[i]) for seed in group):
                group.append(remaining.pop(i))
            else:
                i += 1
        groups.append(group)
    return groups


def select_pullback_entry_pools(
    pools_15m: list[PoolRecord],
    *,
    price: float,
    approach_at: datetime,
    direction: str,
    atr: float,
) -> list[tuple[PoolRecord, str, float]]:
    """Return (pool, selection_reason, limit_entry_price) for each episode."""
    side = "ASK" if direction == "SHORT" else "BID"
    eligible = [
        p
        for p in pools_15m
        if p.side == side
        and p.is_known_before(approach_at)
        and _pool_reachability(price, p, direction=direction, atr=atr)
    ]
    if not eligible:
        return []
    groups = group_overlapping_pools(eligible)
    out: list[tuple[PoolRecord, str, float]] = []
    for group in groups:
        pool, reason = _pick_representative(group, price=price, direction=direction)
        out.append((pool, reason, pullback_limit_price(pool, direction=direction)))
    return out


def selection_audit_row(
    *,
    pool: PoolRecord,
    reason: str,
    limit_px: float,
    approach_at: datetime,
    price: float,
) -> dict[str, Any]:
    return {
        "pool_id": pool.pool_id,
        "timeframe": pool.timeframe,
        "side": pool.side,
        "component_count": pool.component_count,
        "lower_edge": pool.lower_edge,
        "upper_edge": pool.upper_edge,
        "known_at": pool.known_at.isoformat(),
        "selection_reason": reason,
        "limit_entry_price": limit_px,
        "approach_at": approach_at.isoformat(),
        "price_at_selection": price,
        "is_individual": pool.component_count == 1 and str(pool.pool_id).startswith("lld:"),
    }

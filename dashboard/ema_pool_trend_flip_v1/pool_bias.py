"""Pool bias from causal BigBeluga clusters. Uses planner clustering; no copied pool engine."""

from __future__ import annotations

import math
from typing import Any

from .config import (
    POOL_BIAS_CLUSTER_COUNT_WEIGHT,
    POOL_BIAS_DISTANCE_HALFLIFE_PCT,
    POOL_BIAS_MIN_RATIO,
)


def _ensure_planner() -> None:
    import sys

    from .config import planner_root

    root = str(planner_root())
    if root not in sys.path:
        sys.path.insert(0, root)


def cluster_rows(pools: list, entry: float) -> list[dict[str, Any]]:
    _ensure_planner()
    from research.liquidity.order_planner import Side, build_clusters

    uppers = [p for p in pools if getattr(p.side, "value", p.side) == Side.UPPER.value]
    lowers = [p for p in pools if getattr(p.side, "value", p.side) == Side.LOWER.value]
    clusters = build_clusters(uppers, entry) + build_clusters(lowers, entry)
    return [c.to_dict() for c in clusters]


def _score_clusters(clusters: list[dict[str, Any]], *, above: bool, entry: float) -> dict[str, Any]:
    selected = []
    for c in clusters:
        if above and float(c["bottom"]) > entry:
            selected.append(c)
        if (not above) and float(c["top"]) < entry:
            selected.append(c)
    strength_sum = 0.0
    score = 0.0
    nearest = None
    for c in selected:
        dist = abs(float(c["distance_from_entry_pct"]))
        w = math.exp(-dist / max(1e-9, POOL_BIAS_DISTANCE_HALFLIFE_PCT))
        ssum = float(c.get("strength_sum") or 0.0)
        pc = int(c.get("pool_count") or 1)
        score += ssum * w * (1.0 + POOL_BIAS_CLUSTER_COUNT_WEIGHT * max(0, pc - 1))
        strength_sum += ssum
        if nearest is None or dist < nearest:
            nearest = dist
    return {
        "count": len(selected),
        "strength_sum": strength_sum,
        "nearest_distance_pct": nearest,
        "bias_score": score,
        "clusters": selected,
    }


def pool_context(pools: list, entry: float) -> dict[str, Any]:
    clusters = cluster_rows(pools, entry)
    upper = _score_clusters(clusters, above=True, entry=entry)
    lower = _score_clusters(clusters, above=False, entry=entry)
    bullish = (
        upper["bias_score"] > lower["bias_score"] * POOL_BIAS_MIN_RATIO
        and lower["count"] >= 1
        and upper["bias_score"] > 0
    )
    bearish = (
        lower["bias_score"] > upper["bias_score"] * POOL_BIAS_MIN_RATIO
        and upper["count"] >= 1
        and lower["bias_score"] > 0
    )
    return {
        "upper_pool_count": upper["count"],
        "lower_pool_count": lower["count"],
        "upper_pool_strength_sum": upper["strength_sum"],
        "lower_pool_strength_sum": lower["strength_sum"],
        "nearest_upper_pool_distance_pct": upper["nearest_distance_pct"],
        "nearest_lower_pool_distance_pct": lower["nearest_distance_pct"],
        "upper_pool_bias_score": upper["bias_score"],
        "lower_pool_bias_score": lower["bias_score"],
        "bullish_pool_context": bullish,
        "bearish_pool_context": bearish,
        "clusters": clusters,
    }

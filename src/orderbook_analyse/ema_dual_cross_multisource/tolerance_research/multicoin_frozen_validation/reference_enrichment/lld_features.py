"""LLD / liquidity pool features — excluded unless causality is proven."""

from __future__ import annotations

from datetime import datetime

from .causality import as_utc
from .feature_value import FeatureValue, missing

LLD_NAMES = [
    "nearest_support_distance_pct",
    "nearest_resistance_distance_pct",
    "nearest_directional_pool_distance_pct",
    "nearest_adverse_pool_distance_pct",
    "directional_pool_strength",
    "adverse_pool_strength",
    "directional_pool_count",
    "adverse_pool_count",
    "pool_distance_atr",
    "liquidity_asymmetry",
]


def compute_lld_features(
    decision_at: datetime | str,
    *,
    pools: object | None = None,
    causality_proven: bool = False,
) -> dict[str, FeatureValue]:
    """Always CAUSALITY_UNPROVEN unless caller passes causality_proven=True with causal pools.

    Default research path sets all LLD features to null — pool detectors may repaint.
    """
    dec = as_utc(decision_at)
    if not causality_proven or pools is None:
        return {
            n: missing(
                n,
                reason="Pool formation timestamp not proven <= decision_at; repaint risk",
                status="CAUSALITY_UNPROVEN",
                source="lld",
                asof=dec,
                causal=False,
            )
            for n in LLD_NAMES
        }
    # Reserved for future proven causal LLD; still require non-empty causal pools
    return {
        n: missing(n, reason="CAUSAL_LLD_NOT_IMPLEMENTED", status="NOT_AVAILABLE", source="lld", asof=dec, causal=True)
        for n in LLD_NAMES
    }

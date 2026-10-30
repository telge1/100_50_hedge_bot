"""Deterministic lower-pool strength metrics (Phase 2D research)."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SHORT_PKG = ROOT / "tools/phase_short_main_v2_combined_exit_v1"
if str(SHORT_PKG) not in sys.path:
    sys.path.insert(0, str(SHORT_PKG))

from lower_pool_gap_tp_v1 import AtomicLowerBandV1, LowerRegionV1

# Documented thresholds — not tuned to a single chart screenshot.
MIN_REGION_WIDTH_PCT = 0.10
MIN_SINGLE_ATOMIC_WIDTH_PCT = 0.12
MIN_DISTINCT_ORIGINS_STRONG = 2
MIN_ATOMIC_ORIGINS_FOR_NARROW = 3
MIN_PERSISTENCE_BARS_HINT = 5


@dataclass(frozen=True)
class RegionStrengthAuditV1:
    width_pct: float
    distinct_origin_count: int
    atomic_count: int
    min_atomic_unique_origins: int
    max_atomic_unique_origins: int
    origin_density_per_pct_width: float
    min_atomic_width_pct: float
    persistence_bars_min_atomic: int
    qualified: bool
    disqualify_reason: str | None
    score: float


def _region_width_pct(region: LowerRegionV1) -> float:
    mid = (region.lower_edge + region.upper_edge) / 2.0
    if mid <= 0:
        return 0.0
    return (region.upper_edge - region.lower_edge) / mid * 100.0


def _atomic_width_pct(a: AtomicLowerBandV1) -> float:
    mid = (a.lower_edge + a.upper_edge) / 2.0
    if mid <= 0:
        return 0.0
    return (a.upper_edge - a.lower_edge) / mid * 100.0


def evaluate_region_strength(
    region: LowerRegionV1,
    *,
    persistence_bars_by_pool_key: dict[str, int] | None = None,
) -> RegionStrengthAuditV1:
    width = _region_width_pct(region)
    distinct = len(region.distinct_origin_ids)
    atomics = region.atomics or []
    atomic_n = len(atomics)
    min_aw = min((_atomic_width_pct(a) for a in atomics), default=width)
    min_u = min((a.unique_origin_count for a in atomics), default=0)
    max_u = max((a.unique_origin_count for a in atomics), default=0)
    density = distinct / max(width, 0.01)
    streaks = persistence_bars_by_pool_key or {}
    persist = min((streaks.get(a.pool_key, 0) for a in atomics), default=0)

    qualified = True
    reason: str | None = None
    if distinct < MIN_DISTINCT_ORIGINS_STRONG:
        qualified, reason = False, "TOO_FEW_DISTINCT_ORIGINS"
    elif width + 1e-12 < MIN_REGION_WIDTH_PCT:
        if atomic_n == 1 and min_u < MIN_ATOMIC_ORIGINS_FOR_NARROW:
            qualified, reason = False, "NARROW_SINGLE_ATOMIC_WEAK_ORIGINS"
        elif width + 1e-12 < MIN_REGION_WIDTH_PCT * 0.5:
            qualified, reason = False, "REGION_TOO_NARROW"

    score = (
        1000.0 * distinct
        + 100.0 * atomic_n
        + 10.0 * max_u
        + 5.0 * width
        + 2.0 * density
        + 0.5 * persist
    )

    return RegionStrengthAuditV1(
        width_pct=round(width, 8),
        distinct_origin_count=distinct,
        atomic_count=atomic_n,
        min_atomic_unique_origins=min_u,
        max_atomic_unique_origins=max_u,
        origin_density_per_pct_width=round(density, 8),
        min_atomic_width_pct=round(min_aw, 8),
        persistence_bars_min_atomic=persist,
        qualified=qualified,
        disqualify_reason=reason,
        score=round(score, 6),
    )


def is_weak_intermediate_region(region: LowerRegionV1, audit: RegionStrengthAuditV1) -> bool:
    """Pools skipped when resolving Q — same objective rules as strength disqualify."""
    return not audit.qualified

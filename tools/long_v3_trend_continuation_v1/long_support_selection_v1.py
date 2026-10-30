"""Support region selection — union geometry shared with Short lower-pool gap TP."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SHORT_PKG = ROOT / "tools/phase_short_main_v2_combined_exit_v1"
if str(SHORT_PKG) not in sys.path:
    sys.path.insert(0, str(SHORT_PKG))

from lower_pool_gap_tp_v1 import (  # noqa: E402
    LowerRegionV1,
    active_atomic_lowers_from_snapshot,
    gap_pct_between_regions,
    merge_atomic_lowers_into_regions,
)

from contracts_v1 import MIN_QUALIFIED_LOWER_GAP_PCT, MIN_UNIQUE_ORIGINS  # noqa: E402
from long_pool_identity_v1 import lower_pool_still_active  # noqa: E402
from long_risk_audit_v1 import structural_long_sl  # noqa: E402
from weak_pool_audit_v1 import tag_weak_intermediate_pools  # noqa: E402


@dataclass(frozen=True)
class SelectedSupportV1:
    region: LowerRegionV1
    qualified_gap_pct: float
    actual_gap_pct: float | None
    deeper_region: LowerRegionV1 | None
    entry_limit: float
    structural_sl: float
    region_key: str
    tie_break: str


def _region_key(r: LowerRegionV1) -> str:
    return f"{r.lower_edge}|{r.upper_edge}"


def _stacked_regions(snapshot: dict[str, Any]) -> list[LowerRegionV1]:
    atomics = active_atomic_lowers_from_snapshot(snapshot, min_unique_origins=MIN_UNIQUE_ORIGINS)
    return merge_atomic_lowers_into_regions(atomics)


def _actual_gap_pct(p: LowerRegionV1, atomics_sorted_low: list) -> float | None:
    p_lo = p.lower_edge
    below = [a for a in atomics_sorted_low if a.upper_edge < p_lo - 1e-12]
    if not below:
        return None
    below.sort(key=lambda a: -a.upper_edge)
    return (p_lo - below[0].upper_edge) / p_lo * 100.0


def select_support_below_price(
    snapshot: dict[str, Any],
    *,
    market_price: float,
    gap_threshold_pct: float = MIN_QUALIFIED_LOWER_GAP_PCT,
    apply_weak_pool_filter: bool = False,
) -> tuple[SelectedSupportV1 | None, str | None]:
    """V1 baseline: all qualified regions; optional weak filter disabled by default."""
    atomics = active_atomic_lowers_from_snapshot(snapshot, min_unique_origins=MIN_UNIQUE_ORIGINS)
    if apply_weak_pool_filter:
        return None, "WEAK_POOL_FILTER_NOT_ENABLED_V1"

    tag_weak_intermediate_pools(snapshot)  # audit only
    regions = merge_atomic_lowers_into_regions(atomics)
    if not regions:
        return None, "NO_ACTIVE_LOWER_REGIONS"

    stacked = sorted(regions, key=lambda r: (-r.upper_edge, -r.lower_edge))
    candidates: list[tuple[LowerRegionV1, float, LowerRegionV1 | None]] = []
    for i, p in enumerate(stacked):
        if i + 1 >= len(stacked):
            continue
        q = stacked[i + 1]
        g = gap_pct_between_regions(p, q)
        if g + 1e-12 >= gap_threshold_pct:
            candidates.append((p, g, q))

    below = [c for c in candidates if c[0].upper_edge < market_price - 1e-12]
    if not below:
        return None, "NO_QUALIFIED_SUPPORT_BELOW_PRICE"

    def sort_key(item: tuple[LowerRegionV1, float, LowerRegionV1 | None]) -> tuple:
        p = item[0]
        lead = sorted(p.atomics, key=lambda a: a.pool_key)[0].pool_key
        return (-p.upper_edge, p.lower_edge, lead)

    below.sort(key=sort_key)
    p, g, q = below[0]
    tie = "max_upper_then_min_lower_wider_region_then_pool_key"
    act = _actual_gap_pct(p, atomics)
    sl = structural_long_sl(p.lower_edge)
    return (
        SelectedSupportV1(
            region=p,
            qualified_gap_pct=round(g, 8),
            actual_gap_pct=round(act, 8) if act is not None else None,
            deeper_region=q,
            entry_limit=p.upper_edge,
            structural_sl=sl,
            region_key=_region_key(p),
            tie_break=tie,
        ),
        None,
    )


def region_known_in_snapshot(region: LowerRegionV1, snapshot: dict[str, Any]) -> bool:
    """Pool geometry must be present in as-of snapshot (all atomics ACTIVE)."""
    atomics = active_atomic_lowers_from_snapshot(snapshot, min_unique_origins=MIN_UNIQUE_ORIGINS)
    keys = {a.pool_key for a in atomics}
    for a in region.atomics:
        if a.pool_key not in keys:
            return False
    return True


def validate_frozen_support_region(
    snapshot: dict[str, Any],
    *,
    region_lower: float,
    region_upper: float,
    gap_threshold_pct: float = MIN_QUALIFIED_LOWER_GAP_PCT,
) -> tuple[bool, str | None]:
    if not lower_pool_still_active(
        pool_lo=region_lower,
        pool_hi=region_upper,
        origin_ids="",
        snapshot=snapshot,
    ):
        # try any matching band by edges
        found = False
        for b in snapshot.get("lower_bands") or []:
            if str(b.get("pool_status") or "").upper() != "ACTIVE":
                continue
            lo, hi = float(b["lower_price"]), float(b["upper_price"])
            if abs(lo - region_lower) < 1e-9 and abs(hi - region_upper) < 1e-9:
                found = True
                break
        if not found:
            return False, "SUPPORT_DEACTIVATED"

    atomics = active_atomic_lowers_from_snapshot(snapshot, min_unique_origins=MIN_UNIQUE_ORIGINS)
    regions = merge_atomic_lowers_into_regions(atomics)
    target = None
    for r in regions:
        if abs(r.lower_edge - region_lower) < 1e-9 and abs(r.upper_edge - region_upper) < 1e-9:
            target = r
            break
    if target is None:
        return False, "REGION_NOT_FOUND"

    stacked = sorted(regions, key=lambda r: (-r.upper_edge, -r.lower_edge))
    idx = next(i for i, r in enumerate(stacked) if r is target)
    if idx + 1 >= len(stacked):
        return False, "NO_DEEPER_QUALIFIED_LOWER"
    q = stacked[idx + 1]
    g = gap_pct_between_regions(target, q)
    if g + 1e-12 < gap_threshold_pct:
        return False, "GAP_LOST"
    return True, None


def support_still_valid(
    snapshot: dict[str, Any],
    *,
    region_lower: float,
    region_upper: float,
    gap_threshold_pct: float = MIN_QUALIFIED_LOWER_GAP_PCT,
) -> tuple[bool, str | None]:
    return validate_frozen_support_region(
        snapshot,
        region_lower=region_lower,
        region_upper=region_upper,
        gap_threshold_pct=gap_threshold_pct,
    )

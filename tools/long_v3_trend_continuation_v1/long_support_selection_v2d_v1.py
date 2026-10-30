"""Support selection V2D — strong lowers, separate entry P and SL pool Q."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
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
from long_research_config_v2d_v1 import LongResearchConfigV2DV1  # noqa: E402
from long_risk_audit_v1 import structural_long_sl  # noqa: E402
from long_support_selection_v1 import (  # noqa: E402
    SelectedSupportV1,
    _actual_gap_pct,
    _region_key,
    region_known_in_snapshot,
    select_support_below_price,
)
from long_support_strength_v2d_v1 import (  # noqa: E402
    evaluate_region_strength,
    is_weak_intermediate_region,
)
from weak_pool_audit_v1 import tag_weak_intermediate_pools  # noqa: E402


@dataclass(frozen=True)
class SelectedSupportV2DV1:
    region: LowerRegionV1
    sl_region: LowerRegionV1
    qualified_gap_pct: float
    actual_gap_pct: float | None
    entry_limit: float
    structural_sl: float
    sl_reference_mode: str
    region_key: str
    sl_region_key: str
    tie_break: str
    strength_audit: dict[str, Any]
    sl_pool_strength_audit: dict[str, Any]
    skipped_intermediate_pools: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    lineage_id: str | None = None


def _stacked_regions(snapshot: dict[str, Any]) -> list[LowerRegionV1]:
    atomics = active_atomic_lowers_from_snapshot(snapshot, min_unique_origins=MIN_UNIQUE_ORIGINS)
    return sorted(merge_atomic_lowers_into_regions(atomics), key=lambda r: (-r.upper_edge, -r.lower_edge))


def resolve_deeper_sl_pool_q(
    p: LowerRegionV1,
    stacked: list[LowerRegionV1],
    *,
    gap_threshold_pct: float,
    persistence_bars_by_pool_key: dict[str, int] | None,
    skip_weak_intermediate: bool,
) -> tuple[LowerRegionV1 | None, float | None, tuple[dict[str, Any], ...]]:
    """Next deeper qualified Q with gap >= threshold; optionally skip weak stack neighbors."""
    skipped: list[dict[str, Any]] = []
    try:
        idx = next(i for i, r in enumerate(stacked) if _region_key(r) == _region_key(p))
    except StopIteration:
        return None, None, tuple(skipped)

    chosen: LowerRegionV1 | None = None
    chosen_gap: float | None = None
    for j in range(idx + 1, len(stacked)):
        q = stacked[j]
        sa = evaluate_region_strength(q, persistence_bars_by_pool_key=persistence_bars_by_pool_key)
        g = gap_pct_between_regions(p, q)
        if skip_weak_intermediate and is_weak_intermediate_region(q, sa):
            skipped.append(
                {
                    "pool": _region_key(q),
                    "reason": "WEAK_INTERMEDIATE_SKIPPED",
                    "gap_pct_from_p": round(g, 8),
                    "strength": sa.__dict__,
                }
            )
            continue
        if not sa.qualified:
            skipped.append(
                {
                    "pool": _region_key(q),
                    "reason": sa.disqualify_reason or "NOT_QUALIFIED",
                    "gap_pct_from_p": round(g, 8),
                }
            )
            continue
        if g + 1e-12 >= gap_threshold_pct:
            chosen, chosen_gap = q, g
            break
        skipped.append(
            {
                "pool": _region_key(q),
                "reason": "GAP_BELOW_THRESHOLD",
                "gap_pct_from_p": round(g, 8),
            }
        )
    return chosen, chosen_gap, tuple(skipped)


def _sl_price(
    *,
    p: LowerRegionV1,
    q: LowerRegionV1,
    sl_mode: str,
) -> tuple[float, str]:
    if sl_mode == "DEEPER_Q_UPPER":
        return q.upper_edge, "DEEPER_Q_UPPER"
    return structural_long_sl(p.lower_edge), "LEGACY_BELOW_P_LOWER"


def select_support_v2d(
    snapshot: dict[str, Any],
    *,
    market_price: float,
    config: LongResearchConfigV2DV1,
    gap_threshold_pct: float = MIN_QUALIFIED_LOWER_GAP_PCT,
    persistence_bars_by_pool_key: dict[str, int] | None = None,
    session: Any | None = None,
    decision_close_utc: str | None = None,
    prices: dict[str, dict[str, float]] | None = None,
    streak_cache: dict | None = None,
    bar_index: int | None = None,
    closes: list[str] | None = None,
    pool_age_semantics: str | None = None,
    lineage_tracker: Any | None = None,
    formation_semantics: str | None = None,
) -> tuple[SelectedSupportV2DV1 | None, str | None]:
    if config.support_mode == "V2E_QUALIFIED":
        from long_support_selection_v2e_v1 import select_support_v2e

        if session is None or decision_close_utc is None or prices is None or config.v2e_yaml is None:
            return None, "V2E_CONTEXT_MISSING"
        sl_mode = "DEEPER_Q_UPPER" if config.sl_mode == "DEEPER_Q_UPPER" else "LEGACY_BELOW_P_LOWER"
        sel, reason, _ = select_support_v2e(
            session,
            decision_close_utc=decision_close_utc,
            market_price=market_price,
            prices=prices,
            yaml=config.v2e_yaml,
            sl_mode=sl_mode,
            live_consecutive_streak=streak_cache,
            bar_index=bar_index,
            closes=closes,
            pool_age_semantics=pool_age_semantics or config.pool_age_semantics,
            lineage_tracker=lineage_tracker,
            formation_semantics=formation_semantics or config.formation_semantics,
        )
        return sel, reason

    if config.support_mode == "V1_BASELINE":
        base, reason = select_support_below_price(snapshot, market_price=market_price, gap_threshold_pct=gap_threshold_pct)
        if base is None:
            return None, reason
        atomics = active_atomic_lowers_from_snapshot(snapshot, min_unique_origins=MIN_UNIQUE_ORIGINS)
        stacked = _stacked_regions(snapshot)
        q = base.deeper_region
        if q is None:
            return None, "NO_DEEPER_REGION_V1"
        if config.sl_mode == "DEEPER_Q_UPPER":
            sl, sl_ref = q.upper_edge, "DEEPER_Q_UPPER"
        else:
            sl, sl_ref = base.structural_sl, "LEGACY_BELOW_P_LOWER"
        pa = evaluate_region_strength(base.region, persistence_bars_by_pool_key=persistence_bars_by_pool_key)
        qa = evaluate_region_strength(q, persistence_bars_by_pool_key=persistence_bars_by_pool_key)
        return (
            SelectedSupportV2DV1(
                region=base.region,
                sl_region=q,
                qualified_gap_pct=base.qualified_gap_pct,
                actual_gap_pct=base.actual_gap_pct,
                entry_limit=base.entry_limit,
                structural_sl=sl,
                sl_reference_mode=sl_ref,
                region_key=base.region_key,
                sl_region_key=_region_key(q),
                tie_break=base.tie_break,
                strength_audit=pa.__dict__,
                sl_pool_strength_audit=qa.__dict__,
                skipped_intermediate_pools=(),
            ),
            None,
        )

    tag_weak_intermediate_pools(snapshot)
    atomics = active_atomic_lowers_from_snapshot(snapshot, min_unique_origins=MIN_UNIQUE_ORIGINS)
    stacked = _stacked_regions(snapshot)
    if not stacked:
        return None, "NO_ACTIVE_LOWER_REGIONS"

    candidates: list[tuple[SelectedSupportV2DV1, float]] = []
    for p in stacked:
        if p.upper_edge >= market_price - 1e-12:
            continue
        pa = evaluate_region_strength(p, persistence_bars_by_pool_key=persistence_bars_by_pool_key)
        if not pa.qualified:
            continue
        q, g, skipped = resolve_deeper_sl_pool_q(
            p,
            stacked,
            gap_threshold_pct=gap_threshold_pct,
            persistence_bars_by_pool_key=persistence_bars_by_pool_key,
            skip_weak_intermediate=True,
        )
        if q is None or g is None:
            continue
        qa = evaluate_region_strength(q, persistence_bars_by_pool_key=persistence_bars_by_pool_key)
        sl, sl_ref = _sl_price(p=p, q=q, sl_mode=config.sl_mode)
        if sl >= market_price - 1e-12:
            continue
        act = _actual_gap_pct(p, atomics)
        sel = SelectedSupportV2DV1(
            region=p,
            sl_region=q,
            qualified_gap_pct=round(g, 8),
            actual_gap_pct=round(act, 8) if act is not None else None,
            entry_limit=p.upper_edge,
            structural_sl=sl,
            sl_reference_mode=sl_ref,
            region_key=_region_key(p),
            sl_region_key=_region_key(q),
            tie_break="max_strength_score_then_max_upper_then_pool_key",
            strength_audit=pa.__dict__,
            sl_pool_strength_audit=qa.__dict__,
            skipped_intermediate_pools=skipped,
        )
        candidates.append((sel, pa.score))

    if not candidates:
        return None, "NO_STRONG_QUALIFIED_SUPPORT_BELOW_PRICE"

    def sort_key(item: tuple[SelectedSupportV2DV1, float]) -> tuple:
        sel, score = item
        lead = sorted(sel.region.atomics, key=lambda a: a.pool_key)[0].pool_key
        return (-score, -sel.region.upper_edge, sel.region.lower_edge, lead)

    candidates.sort(key=sort_key)
    return candidates[0][0], None


def v2d_as_v1_compat(sel: SelectedSupportV2DV1) -> SelectedSupportV1:
    return SelectedSupportV1(
        region=sel.region,
        qualified_gap_pct=sel.qualified_gap_pct,
        actual_gap_pct=sel.actual_gap_pct,
        deeper_region=sel.sl_region,
        entry_limit=sel.entry_limit,
        structural_sl=sel.structural_sl,
        region_key=sel.region_key,
        tie_break=sel.tie_break,
    )


def validate_frozen_support_v2d(
    snapshot: dict[str, Any],
    *,
    region_lower: float,
    region_upper: float,
    config: LongResearchConfigV2DV1,
    gap_threshold_pct: float = MIN_QUALIFIED_LOWER_GAP_PCT,
    persistence_bars_by_pool_key: dict[str, int] | None = None,
    session: Any | None = None,
    decision_close_utc: str | None = None,
    market_close: float | None = None,
    prices: dict[str, dict[str, float]] | None = None,
    streak_cache: dict | None = None,
    bar_index: int | None = None,
    closes: list[str] | None = None,
    pool_age_semantics: str | None = None,
    lineage_tracker: Any | None = None,
    formation_semantics: str | None = None,
    frozen_lineage_id: str | None = None,
    touch_lifecycle: str | None = None,
) -> tuple[bool, str | None]:
    if config.support_mode == "V2E_QUALIFIED":
        from long_support_selection_v2e_v1 import validate_frozen_region_v2e

        if (
            session is None
            or decision_close_utc is None
            or prices is None
            or config.v2e_yaml is None
            or market_close is None
        ):
            return False, "V2E_CONTEXT_MISSING"
        return validate_frozen_region_v2e(
            session,
            decision_close_utc=decision_close_utc,
            region_lower=region_lower,
            region_upper=region_upper,
            market_close=market_close,
            prices=prices,
            yaml=config.v2e_yaml,
            gap_threshold_pct=gap_threshold_pct,
            live_consecutive_streak=streak_cache,
            bar_index=bar_index,
            closes=closes,
            pool_age_semantics=pool_age_semantics or config.pool_age_semantics,
            lineage_tracker=lineage_tracker,
            formation_semantics=formation_semantics or config.formation_semantics,
            frozen_lineage_id=frozen_lineage_id,
            touch_lifecycle=touch_lifecycle or config.touch_lifecycle,
        )

    stacked = _stacked_regions(snapshot)
    target = None
    for r in stacked:
        if abs(r.lower_edge - region_lower) < 1e-9 and abs(r.upper_edge - region_upper) < 1e-9:
            target = r
            break
    if target is None:
        return False, "REGION_NOT_FOUND"
    if not region_known_in_snapshot(target, snapshot):
        return False, "SUPPORT_DEACTIVATED"
    q, g, _ = resolve_deeper_sl_pool_q(
        target,
        stacked,
        gap_threshold_pct=gap_threshold_pct,
        persistence_bars_by_pool_key=persistence_bars_by_pool_key,
        skip_weak_intermediate=config.support_mode == "V2D_STRONG",
    )
    if q is None or g is None:
        return False, "NO_DEEPER_QUALIFIED_LOWER"
    if g + 1e-12 < gap_threshold_pct:
        return False, "GAP_LOST"
    return True, None


def explain_v1_prefers_narrow_pool(snapshot: dict[str, Any], market_price: float) -> dict[str, Any]:
    """Document why baseline sorts highest upper edge first."""
    stacked = _stacked_regions(snapshot)
    atomics = active_atomic_lowers_from_snapshot(snapshot, min_unique_origins=MIN_UNIQUE_ORIGINS)
    rows = []
    for p in stacked:
        if p.upper_edge >= market_price - 1e-12:
            continue
        idx = stacked.index(p)
        if idx + 1 >= len(stacked):
            continue
        q = stacked[idx + 1]
        g = gap_pct_between_regions(p, q)
        if g + 1e-12 < MIN_QUALIFIED_LOWER_GAP_PCT:
            continue
        pa = evaluate_region_strength(p)
        rows.append(
            {
                "region_key": _region_key(p),
                "upper": p.upper_edge,
                "lower": p.lower_edge,
                "width_pct": pa.width_pct,
                "distinct_origins": pa.distinct_origin_count,
                "strength_score": pa.score,
                "v1_sort_key": (-p.upper_edge, p.lower_edge),
            }
        )
    rows.sort(key=lambda r: (r["v1_sort_key"][0], r["v1_sort_key"][1]))
    winner = rows[0] if rows else None
    strong_sorted = sorted(rows, key=lambda r: (-r["strength_score"], -r["upper"]))
    return {
        "market_price": market_price,
        "v1_winner": winner,
        "v2d_strong_winner": strong_sorted[0] if strong_sorted else None,
        "all_gap_qualified_below_price": rows,
    }

"""V2E qualified lower support — age, height, formation, nearest-upper selection."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

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

from long_config_yaml_v1 import LongSupportQualificationYamlV1  # noqa: E402
from long_support_selection_v2d_v1 import SelectedSupportV2DV1, _region_key  # noqa: E402
from pool_age_consecutive_lower_v1 import streak_start_close_utc  # noqa: E402


@dataclass(frozen=True)
class AtomicQualificationAuditV1:
    pool_key: str
    consecutive_active_bars: int
    age_ok: bool
    formation_below_price_ok: bool
    formation_reference_close: float | None
    atomic_upper_at_formation: float


@dataclass(frozen=True)
class RegionQualificationAuditV1:
    region_key: str
    pool_height_pct: float
    height_ok: bool
    below_price_ok: bool
    qualified: bool
    disqualify_reason: str | None
    atomics: tuple[AtomicQualificationAuditV1, ...] = field(default_factory=tuple)


def region_pool_height_pct(region: LowerRegionV1) -> float:
    lo = region.lower_edge
    if lo <= 0:
        return 0.0
    return (region.upper_edge - region.lower_edge) / lo * 100.0


from pool_age_consecutive_lower_v1 import streak_start_close_utc  # noqa: E402
from pool_consecutive_streak_v2e_v1 import streak_for_band  # noqa: E402

PoolAgeSemanticsV2H = Literal["EXACT_ATOMIC_5", "CAUSAL_REGION_LINEAGE_5"]
FormationSemanticsV2I = Literal["LEGACY_LINEAGE_START", "CAUSAL_BELOW_PRICE_SEGMENT", "EXACT_ATOMIC_DEFAULT"]

_AGE_KW_DEFAULTS = {
    "pool_age_semantics": "EXACT_ATOMIC_5",
    "lineage_tracker": None,
}


def _age_kwargs(
    pool_age_semantics: PoolAgeSemanticsV2H = "EXACT_ATOMIC_5",
    lineage_tracker: Any | None = None,
) -> dict[str, Any]:
    return {"pool_age_semantics": pool_age_semantics, "lineage_tracker": lineage_tracker}


def _audit_atomic(
    session,
    *,
    decision_close_utc: str,
    atomic_lo: float,
    atomic_hi: float,
    prices: dict[str, dict[str, float]],
    yaml: LongSupportQualificationYamlV1,
    live_consecutive_streak: dict[str, int] | None = None,
    bar_index: int | None = None,
    closes: list[str] | None = None,
) -> AtomicQualificationAuditV1:
    pk = f"{atomic_lo}|{atomic_hi}"
    if live_consecutive_streak is not None:
        streak = streak_for_band(live_consecutive_streak, atomic_lo, atomic_hi)
        complete = True
    else:
        from pool_age_consecutive_lower_v1 import consecutive_active_streak_lower

        streak, complete, _ = consecutive_active_streak_lower(
            session,
            decision_close_utc=decision_close_utc,
            pool_lo=atomic_lo,
            pool_hi=atomic_hi,
            min_unique_origins=yaml.min_unique_origins,
        )
    age_ok = complete and streak >= yaml.min_pool_age_bars
    form_ok = True
    ref_close: float | None = None
    if yaml.require_formation_below_price and streak > 0 and complete:
        if live_consecutive_streak is not None and bar_index is not None and closes is not None:
            start = closes[bar_index - streak + 1]
        else:
            start = streak_start_close_utc(decision_close_utc, streak)
        o = prices.get(start)
        if o is None:
            form_ok = False
        else:
            ref_close = float(o["c"])
            form_ok = atomic_hi < ref_close - 1e-12
    elif yaml.require_formation_below_price and not complete:
        form_ok = False
    return AtomicQualificationAuditV1(
        pool_key=pk,
        consecutive_active_bars=streak if complete else 0,
        age_ok=age_ok,
        formation_below_price_ok=form_ok,
        formation_reference_close=ref_close,
        atomic_upper_at_formation=atomic_hi,
    )


def audit_region_qualification(
    session,
    *,
    region: LowerRegionV1,
    decision_close_utc: str,
    market_close: float,
    prices: dict[str, dict[str, float]],
    yaml: LongSupportQualificationYamlV1,
    live_consecutive_streak: dict[str, int] | None = None,
    bar_index: int | None = None,
    closes: list[str] | None = None,
    pool_age_semantics: PoolAgeSemanticsV2H = "EXACT_ATOMIC_5",
    lineage_tracker: Any | None = None,
    formation_semantics: FormationSemanticsV2I = "EXACT_ATOMIC_DEFAULT",
) -> RegionQualificationAuditV1:
    height = region_pool_height_pct(region)
    height_ok = height + 1e-12 >= yaml.min_pool_height_pct
    below = region.upper_edge < market_close - 1e-12
    atom_audits = tuple(
        _audit_atomic(
            session,
            decision_close_utc=decision_close_utc,
            atomic_lo=a.lower_edge,
            atomic_hi=a.upper_edge,
            prices=prices,
            yaml=yaml,
            live_consecutive_streak=live_consecutive_streak,
            bar_index=bar_index,
            closes=closes,
        )
        for a in region.atomics
    )
    reason: str | None = None
    qualified = True
    if not below:
        qualified, reason = False, "NOT_BELOW_MARKET_CLOSE"
    elif not height_ok:
        qualified, reason = False, "HEIGHT_BELOW_MIN"
    elif not atom_audits:
        qualified, reason = False, "NO_ATOMICS"
    elif pool_age_semantics == "CAUSAL_REGION_LINEAGE_5":
        if lineage_tracker is None:
            qualified, reason = False, "LINEAGE_TRACKER_MISSING"
        else:
            streak = lineage_tracker.streak_for_region(region)
            age_ok = streak >= yaml.min_pool_age_bars
            if not age_ok:
                qualified, reason = False, "REGION_LINEAGE_AGE_INSUFFICIENT"
            elif formation_semantics == "CAUSAL_BELOW_PRICE_SEGMENT":
                bp = lineage_tracker.below_price_streak_for_region(region)
                if bp < yaml.min_pool_age_bars:
                    qualified, reason = False, "BELOW_PRICE_STREAK_INSUFFICIENT"
            elif formation_semantics == "LEGACY_LINEAGE_START" and bar_index is not None and closes is not None:
                form_ok, _ = lineage_tracker.formation_legacy_lineage_start(
                    region, bar_index=bar_index, closes=closes, prices=prices
                )
                if not form_ok:
                    qualified, reason = False, "FORMATION_NOT_BELOW_PRICE"
            elif formation_semantics not in ("CAUSAL_BELOW_PRICE_SEGMENT", "LEGACY_LINEAGE_START"):
                qualified, reason = False, "FORMATION_MODE_INVALID"
    else:
        for aa in atom_audits:
            if not aa.age_ok:
                qualified, reason = False, "ATOMIC_AGE_INSUFFICIENT"
                break
            if not aa.formation_below_price_ok:
                qualified, reason = False, "FORMATION_NOT_BELOW_PRICE"
                break
    return RegionQualificationAuditV1(
        region_key=_region_key(region),
        pool_height_pct=round(height, 8),
        height_ok=height_ok,
        below_price_ok=below,
        qualified=qualified,
        disqualify_reason=reason,
        atomics=atom_audits,
    )


def _stacked(snapshot: dict[str, Any], min_unique: int) -> list[LowerRegionV1]:
    atomics = active_atomic_lowers_from_snapshot(snapshot, min_unique_origins=min_unique)
    return sorted(merge_atomic_lowers_into_regions(atomics), key=lambda r: (-r.upper_edge, -r.lower_edge))


def resolve_q_for_p_v2e(
    session,
    *,
    p: LowerRegionV1,
    stacked: list[LowerRegionV1],
    decision_close_utc: str,
    market_close: float,
    prices: dict[str, dict[str, float]],
    yaml: LongSupportQualificationYamlV1,
    live_consecutive_streak: dict[str, int] | None = None,
    bar_index: int | None = None,
    closes: list[str] | None = None,
    pool_age_semantics: PoolAgeSemanticsV2H = "EXACT_ATOMIC_5",
    lineage_tracker: Any | None = None,
    formation_semantics: FormationSemanticsV2I = "EXACT_ATOMIC_DEFAULT",
) -> tuple[LowerRegionV1 | None, float | None, tuple[dict[str, Any], ...]]:
    skipped: list[dict[str, Any]] = []
    try:
        idx = next(i for i, r in enumerate(stacked) if _region_key(r) == _region_key(p))
    except StopIteration:
        return None, None, tuple(skipped)
    for j in range(idx + 1, len(stacked)):
        q = stacked[j]
        qa = audit_region_qualification(
            session,
            region=q,
            decision_close_utc=decision_close_utc,
            market_close=market_close,
            prices=prices,
            yaml=yaml,
            live_consecutive_streak=live_consecutive_streak,
            bar_index=bar_index,
            closes=closes,
            pool_age_semantics=pool_age_semantics,
            lineage_tracker=lineage_tracker,
            formation_semantics=formation_semantics,
        )
        g = gap_pct_between_regions(p, q)
        if not qa.qualified:
            skipped.append(
                {
                    "pool": qa.region_key,
                    "reason": qa.disqualify_reason,
                    "pool_height_pct": qa.pool_height_pct,
                    "gap_pct_from_p": round(g, 8),
                    "atomics": [aa.__dict__ for aa in qa.atomics],
                }
            )
            continue
        if g + 1e-12 >= yaml.lower_gap_threshold_pct:
            return q, g, tuple(skipped)
        skipped.append(
            {
                "pool": qa.region_key,
                "reason": "GAP_BELOW_THRESHOLD",
                "pool_height_pct": qa.pool_height_pct,
                "gap_pct_from_p": round(g, 8),
            }
        )
    return None, None, tuple(skipped)


def select_support_v2e(
    session,
    *,
    decision_close_utc: str,
    market_price: float,
    prices: dict[str, dict[str, float]],
    yaml: LongSupportQualificationYamlV1,
    sl_mode: str = "DEEPER_Q_UPPER",
    live_consecutive_streak: dict[str, int] | None = None,
    bar_index: int | None = None,
    closes: list[str] | None = None,
    pool_age_semantics: PoolAgeSemanticsV2H = "EXACT_ATOMIC_5",
    lineage_tracker: Any | None = None,
    formation_semantics: FormationSemanticsV2I = "EXACT_ATOMIC_DEFAULT",
) -> tuple[SelectedSupportV2DV1 | None, str | None, RegionQualificationAuditV1 | None]:
    stacked = _stacked(session.collect_at_close(decision_close_utc), yaml.min_unique_origins)
    if not stacked:
        return None, "NO_ACTIVE_LOWER_REGIONS", None

    candidates: list[tuple[SelectedSupportV2DV1, RegionQualificationAuditV1]] = []
    for p in stacked:
        if p.upper_edge >= market_price - 1e-12:
            continue
        pa = audit_region_qualification(
            session,
            region=p,
            decision_close_utc=decision_close_utc,
            market_close=market_price,
            prices=prices,
            yaml=yaml,
            live_consecutive_streak=live_consecutive_streak,
            bar_index=bar_index,
            closes=closes,
            pool_age_semantics=pool_age_semantics,
            lineage_tracker=lineage_tracker,
            formation_semantics=formation_semantics,
        )
        if not pa.qualified:
            continue
        q, g, skipped = resolve_q_for_p_v2e(
            session,
            p=p,
            stacked=stacked,
            decision_close_utc=decision_close_utc,
            market_close=market_price,
            prices=prices,
            yaml=yaml,
            live_consecutive_streak=live_consecutive_streak,
            bar_index=bar_index,
            closes=closes,
            pool_age_semantics=pool_age_semantics,
            lineage_tracker=lineage_tracker,
            formation_semantics=formation_semantics,
        )
        if q is None or g is None:
            continue
        sl = q.upper_edge if sl_mode == "DEEPER_Q_UPPER" else p.lower_edge * 0.998
        lid = lineage_tracker.lineage_id_for_region(p) if lineage_tracker else None
        sel = SelectedSupportV2DV1(
            region=p,
            sl_region=q,
            qualified_gap_pct=round(g, 8),
            actual_gap_pct=None,
            entry_limit=p.upper_edge,
            structural_sl=sl,
            sl_reference_mode=sl_mode,
            region_key=_region_key(p),
            sl_region_key=_region_key(q),
            tie_break="nearest_qualified_max_upper_below_price",
            strength_audit={"pool_height_pct": pa.pool_height_pct, "atomics": [a.__dict__ for a in pa.atomics]},
            sl_pool_strength_audit={},
            skipped_intermediate_pools=skipped,
            lineage_id=lid,
        )
        candidates.append((sel, pa))

    if not candidates:
        return None, "NO_V2E_QUALIFIED_SUPPORT", None

    def sort_key(item: tuple[SelectedSupportV2DV1, RegionQualificationAuditV1]) -> tuple:
        sel, _ = item
        lead = sorted(sel.region.atomics, key=lambda a: a.pool_key)[0].pool_key
        return (-sel.region.upper_edge, sel.region.lower_edge, lead)

    candidates.sort(key=sort_key)
    sel, pa = candidates[0]
    return sel, None, pa


def validate_frozen_region_v2e(
    session,
    *,
    decision_close_utc: str,
    region_lower: float,
    region_upper: float,
    market_close: float,
    prices: dict[str, dict[str, float]],
    yaml: LongSupportQualificationYamlV1,
    gap_threshold_pct: float | None = None,
    live_consecutive_streak: dict[str, int] | None = None,
    bar_index: int | None = None,
    closes: list[str] | None = None,
    pool_age_semantics: PoolAgeSemanticsV2H = "EXACT_ATOMIC_5",
    lineage_tracker: Any | None = None,
    formation_semantics: FormationSemanticsV2I = "EXACT_ATOMIC_DEFAULT",
    frozen_lineage_id: str | None = None,
    touch_lifecycle: str = "UNION_KEY_V2I",
) -> tuple[bool, str | None]:
    gap_threshold_pct = gap_threshold_pct or yaml.lower_gap_threshold_pct
    stacked = _stacked(session.collect_at_close(decision_close_utc), yaml.min_unique_origins)
    target = None
    for r in stacked:
        if abs(r.lower_edge - region_lower) < 1e-9 and abs(r.upper_edge - region_upper) < 1e-9:
            target = r
            break
    if target is None and touch_lifecycle == "LINEAGE_AWARE_V2J" and frozen_lineage_id and lineage_tracker:
        from long_lineage_lifecycle_v2j_v1 import resolve_region_by_lineage_at_close

        res = resolve_region_by_lineage_at_close(
            session,
            decision_close_utc=decision_close_utc,
            lineage_id=frozen_lineage_id,
            lineage_tracker=lineage_tracker,
            yaml=yaml,
        )
        if res.region is None:
            return False, res.reason or "REGION_NOT_FOUND"
        target = res.region
    if target is None:
        return False, "REGION_NOT_FOUND"
    pa = audit_region_qualification(
        session,
        region=target,
        decision_close_utc=decision_close_utc,
        market_close=market_close,
        prices=prices,
        yaml=yaml,
        live_consecutive_streak=live_consecutive_streak,
        bar_index=bar_index,
        closes=closes,
        pool_age_semantics=pool_age_semantics,
        lineage_tracker=lineage_tracker,
        formation_semantics=formation_semantics,
    )
    if not pa.qualified:
        return False, pa.disqualify_reason or "NOT_QUALIFIED"
    q, g, _ = resolve_q_for_p_v2e(
        session,
        p=target,
        stacked=stacked,
        decision_close_utc=decision_close_utc,
        market_close=market_close,
        prices=prices,
        yaml=yaml,
        live_consecutive_streak=live_consecutive_streak,
        bar_index=bar_index,
        closes=closes,
        pool_age_semantics=pool_age_semantics,
        lineage_tracker=lineage_tracker,
        formation_semantics=formation_semantics,
    )
    if q is None or g is None:
        return False, "NO_DEEPER_QUALIFIED_LOWER"
    if g + 1e-12 < gap_threshold_pct:
        return False, "GAP_LOST"
    return True, None

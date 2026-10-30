"""Phase 2J — lineage-aware touch, recovery, and pending continuity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from long_config_yaml_v1 import LongSupportQualificationYamlV1
from long_pool_identity_v1 import parse_origin_id_set
from long_support_selection_v2d_v1 import SelectedSupportV2DV1, _region_key
from long_support_selection_v2e_v1 import (
    audit_region_qualification,
    resolve_q_for_p_v2e,
    _stacked,
)


@dataclass(frozen=True)
class LineageRegionResolveV2J:
    region: Any | None
    reason: str | None
    ambiguous: bool = False


def resolve_region_by_lineage_at_close(
    session,
    *,
    decision_close_utc: str,
    lineage_id: str | None,
    lineage_tracker: Any | None,
    yaml: LongSupportQualificationYamlV1,
) -> LineageRegionResolveV2J:
    if not lineage_id or lineage_tracker is None:
        return LineageRegionResolveV2J(None, "NO_LINEAGE_CONTEXT")
    stacked = _stacked(session.collect_at_close(decision_close_utc), yaml.min_unique_origins)
    matches = [r for r in stacked if lineage_tracker.lineage_id_for_region(r) == lineage_id]
    if not matches:
        return LineageRegionResolveV2J(None, "LINEAGE_REGION_ABSENT")
    if len(matches) > 1:
        return LineageRegionResolveV2J(None, "LINEAGE_AMBIGUOUS_MULTIPLE_REGIONS", ambiguous=True)
    for rec in reversed(lineage_tracker.bar_records):
        if rec.bar_utc != decision_close_utc:
            continue
        if rec.lineage_id == lineage_id and rec.match_ambiguous:
            return LineageRegionResolveV2J(None, "LINEAGE_MATCH_AMBIGUOUS", ambiguous=True)
        if rec.lineage_id == lineage_id:
            break
    return LineageRegionResolveV2J(matches[0], None)


def origin_continuity_ok(*, frozen_origin_ids: str, region: Any) -> bool:
    wanted = parse_origin_id_set(frozen_origin_ids)
    if not wanted:
        return True
    present = frozenset(region.distinct_origin_ids)
    return wanted <= present


def lineage_region_active_at_close(
    session,
    *,
    decision_close_utc: str,
    lineage_id: str | None,
    frozen_origin_ids: str,
    lineage_tracker: Any | None,
    yaml: LongSupportQualificationYamlV1,
) -> tuple[bool, str | None, Any | None]:
    res = resolve_region_by_lineage_at_close(
        session,
        decision_close_utc=decision_close_utc,
        lineage_id=lineage_id,
        lineage_tracker=lineage_tracker,
        yaml=yaml,
    )
    if res.region is None:
        return False, res.reason, None
    if not origin_continuity_ok(frozen_origin_ids=frozen_origin_ids, region=res.region):
        return False, "ORIGIN_CONTINUITY_LOST", res.region
    return True, None, res.region


def record_structure_change(
    audit_sink: list[dict[str, Any]] | None,
    *,
    bar: str,
    phase: str,
    old_key: str | None,
    new_key: str | None,
    lineage_id: str | None,
    old_height_pct: float | None,
    new_height_pct: float | None,
    touch_phase: str | None,
    pending_order_id: str | None,
    decision: str,
    reason: str,
    **extra: Any,
) -> None:
    if audit_sink is None:
        return
    audit_sink.append(
        {
            "bar_utc": bar,
            "phase": phase,
            "old_union_key": old_key,
            "new_union_key": new_key,
            "lineage_id": lineage_id,
            "old_pool_height_pct": old_height_pct,
            "new_pool_height_pct": new_height_pct,
            "touch_phase": touch_phase,
            "pending_order_id": pending_order_id,
            "decision": decision,
            "reason": reason,
            **extra,
        }
    )


def materialize_support_v2e_for_region(
    session,
    *,
    region: Any,
    decision_close_utc: str,
    market_close: float,
    prices: dict[str, dict[str, float]],
    yaml: LongSupportQualificationYamlV1,
    sl_mode: str,
    live_consecutive_streak: dict[str, int] | None,
    bar_index: int | None,
    closes: list[str] | None,
    pool_age_semantics: str,
    lineage_tracker: Any | None,
    formation_semantics: str,
) -> tuple[SelectedSupportV2DV1 | None, str | None]:
    stacked = _stacked(session.collect_at_close(decision_close_utc), yaml.min_unique_origins)
    pa = audit_region_qualification(
        session,
        region=region,
        decision_close_utc=decision_close_utc,
        market_close=market_close,
        prices=prices,
        yaml=yaml,
        live_consecutive_streak=live_consecutive_streak,
        bar_index=bar_index,
        closes=closes,
        pool_age_semantics=pool_age_semantics,  # type: ignore[arg-type]
        lineage_tracker=lineage_tracker,
        formation_semantics=formation_semantics,  # type: ignore[arg-type]
    )
    if not pa.qualified:
        return None, pa.disqualify_reason or "NOT_QUALIFIED"
    q, g, skipped = resolve_q_for_p_v2e(
        session,
        p=region,
        stacked=stacked,
        decision_close_utc=decision_close_utc,
        market_close=market_close,
        prices=prices,
        yaml=yaml,
        live_consecutive_streak=live_consecutive_streak,
        bar_index=bar_index,
        closes=closes,
        pool_age_semantics=pool_age_semantics,  # type: ignore[arg-type]
        lineage_tracker=lineage_tracker,
        formation_semantics=formation_semantics,  # type: ignore[arg-type]
    )
    if q is None or g is None:
        return None, "NO_DEEPER_QUALIFIED_LOWER"
    sl = q.upper_edge if sl_mode == "DEEPER_Q_UPPER" else region.lower_edge * 0.998
    lid = lineage_tracker.lineage_id_for_region(region) if lineage_tracker else None
    sel = SelectedSupportV2DV1(
        region=region,
        sl_region=q,
        qualified_gap_pct=round(g, 8),
        actual_gap_pct=None,
        entry_limit=region.upper_edge,
        structural_sl=sl,
        sl_reference_mode=sl_mode,
        region_key=_region_key(region),
        sl_region_key=_region_key(q),
        tie_break="lineage_aware_recovery_materialize",
        strength_audit={"pool_height_pct": pa.pool_height_pct, "atomics": [a.__dict__ for a in pa.atomics]},
        sl_pool_strength_audit={},
        skipped_intermediate_pools=skipped,
        lineage_id=lid,
    )
    return sel, None

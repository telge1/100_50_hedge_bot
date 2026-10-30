"""Phase 2K — dynamic buy limit follows growing P.upper (Variant B)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from long_atomic_tp_v1 import select_long_a_tp_at_fill
from long_lineage_lifecycle_v2j_v1 import (
    lineage_region_active_at_close,
    materialize_support_v2e_for_region,
    record_structure_change,
)
from long_pending_v1 import LongPendingOrderV1, next_5m_open_after_close
from long_research_config_v2d_v1 import LongResearchConfigV2DV1
from long_risk_audit_v1 import risk_pct, sl_active_interval_overlaps
from long_support_selection_v2e_v1 import region_pool_height_pct


def _origin_ids_from_region(region: Any) -> str:
    return "|".join(region.distinct_origin_ids)


def try_dynamic_upper_growth_reprice(
    st: Any,
    *,
    bar: str,
    close: float,
    snap: dict[str, Any],
    config: LongResearchConfigV2DV1,
    session: Any,
    lineage_tracker: Any | None,
    streak_cache: dict | None,
    bar_index: int | None,
    closes: list[str] | None,
    prices: dict[str, dict[str, float]] | None,
    exit_variant: str,
    ev_fn: Any,
    risk_blocked_fn: Any,
    origin_ids_from_support: Any,
) -> None:
    """Variant B: cancel/replace pending limit when same-lineage P upper edge grows."""
    if config.buy_limit_semantics != "DYNAMIC_UPPER_GROWTH_V2K":
        return
    if st.pending is None or st.position is not None:
        return
    if (
        config.touch_lifecycle != "LINEAGE_AWARE_V2J"
        or not st.pending.lineage_id
        or config.v2e_yaml is None
        or session is None
        or prices is None
    ):
        return

    ok_lin, lin_reason, cur_region = lineage_region_active_at_close(
        session,
        decision_close_utc=bar,
        lineage_id=st.pending.lineage_id,
        frozen_origin_ids=st.pending.origin_ids,
        lineage_tracker=lineage_tracker,
        yaml=config.v2e_yaml,
    )
    if not ok_lin or cur_region is None:
        return

    new_upper = float(cur_region.upper_edge)
    old_limit = float(st.pending.planned_entry)
    if new_upper <= old_limit + 1e-12:
        return

    if new_upper >= close - 1e-12:
        record_structure_change(
            st.structure_audits,
            bar=bar,
            phase="PENDING_REPRICE",
            old_key=st.pending.frozen_support_key,
            new_key=f"{cur_region.lower_edge}|{new_upper}",
            lineage_id=st.pending.lineage_id,
            old_height_pct=None,
            new_height_pct=region_pool_height_pct(cur_region),
            touch_phase=None,
            pending_order_id=st.pending.order_id,
            decision="BLOCK",
            reason="REPRICE_BLOCKED_LIMIT_AT_OR_ABOVE_MARKET",
            old_limit=old_limit,
            new_upper=new_upper,
            close=close,
        )
        ev_fn(st, "REPRICE_BLOCKED", bar, reason="LIMIT_AT_OR_ABOVE_MARKET", close=close, new_upper=new_upper)
        return

    refreshed, mat_reason = materialize_support_v2e_for_region(
        session,
        region=cur_region,
        decision_close_utc=bar,
        market_close=close,
        prices=prices,
        yaml=config.v2e_yaml,
        sl_mode=config.sl_mode,
        live_consecutive_streak=streak_cache,
        bar_index=bar_index,
        closes=closes,
        pool_age_semantics=config.pool_age_semantics,
        lineage_tracker=lineage_tracker,
        formation_semantics=config.formation_semantics,
    )
    if refreshed is None:
        ev_fn(st, "REPRICE_BLOCKED", bar, reason=mat_reason or "REQUALIFY_FAILED")
        return

    if refreshed.structural_sl >= refreshed.entry_limit - 1e-12:
        ev_fn(st, "REPRICE_BLOCKED", bar, reason="SL_NOT_BELOW_ENTRY")
        return

    blocked, rp = risk_blocked_fn(refreshed.entry_limit, refreshed.structural_sl, config)
    if blocked:
        ev_fn(st, "REPRICE_SKIPPED_RISK_CAP", bar, risk_pct=rp, cap=config.max_risk_pct)
        return

    if exit_variant == "LONG_A":
        tp_limit, _tp_meta, tp_reason = select_long_a_tp_at_fill(snap, entry_fill=refreshed.entry_limit)
        if tp_limit is None:
            ev_fn(st, "REPRICE_BLOCKED", bar, reason=tp_reason or "NO_LONG_A_TP")
            record_structure_change(
                st.structure_audits,
                bar=bar,
                phase="PENDING_REPRICE",
                old_key=st.pending.pool_key,
                new_key=refreshed.region_key,
                lineage_id=st.pending.lineage_id,
                old_height_pct=None,
                new_height_pct=region_pool_height_pct(cur_region),
                touch_phase=None,
                pending_order_id=st.pending.order_id,
                decision="BLOCK",
                reason="REPRICE_BLOCKED_NO_TP",
            )
            return
        tp_gap_pct = (tp_limit - refreshed.entry_limit) / refreshed.entry_limit * 100.0
    else:
        tp_gap_pct = None

    old_order_id = st.pending.order_id
    old_pool_key = st.pending.pool_key
    entry_change_pct = (refreshed.entry_limit - old_limit) / old_limit * 100.0

    ev_fn(
        st,
        "CANCEL",
        bar,
        reason="DYNAMIC_UPPER_GROWTH_REPRICE",
        order_id=old_order_id,
        old_limit=old_limit,
        new_limit=refreshed.entry_limit,
    )
    st.cancels += 1

    overlap = sl_active_interval_overlaps(snap, refreshed.structural_sl)
    st.pending = LongPendingOrderV1(
        order_id=str(uuid4()),
        decision_close_utc=bar,
        earliest_eligible_execution_utc=next_5m_open_after_close(bar),
        pool_lower=refreshed.region.lower_edge,
        pool_upper=refreshed.region.upper_edge,
        pool_key=refreshed.region_key,
        origin_ids=origin_ids_from_support(refreshed),
        planned_entry=refreshed.entry_limit,
        planned_sl=refreshed.structural_sl,
        selection_reason="DYNAMIC_UPPER_GROWTH_REPRICE",
        lineage_id=refreshed.lineage_id,
        frozen_support_key=refreshed.region_key,
    )
    ev_fn(
        st,
        "PLACE",
        bar,
        limit=refreshed.entry_limit,
        sl=refreshed.structural_sl,
        sl_pool=refreshed.sl_region_key,
        gap_pct=refreshed.qualified_gap_pct,
        risk_pct=rp,
        sl_overlap=overlap.overlapping,
        earliest=st.pending.earliest_eligible_execution_utc,
        order_id=st.pending.order_id,
        lineage_id=refreshed.lineage_id,
        selection_reason="DYNAMIC_UPPER_GROWTH_REPRICE",
        repriced_from_limit=old_limit,
        entry_change_pct=round(entry_change_pct, 8),
        tp_gap_pct=round(tp_gap_pct, 8) if tp_gap_pct is not None else None,
    )
    ev_fn(
        st,
        "REPLACE",
        bar,
        replace_kind="DYNAMIC_UPPER_GROWTH_V2K",
        old_pool=old_pool_key,
        new_pool=refreshed.region_key,
        old_limit=old_limit,
        new_limit=refreshed.entry_limit,
        new_sl=refreshed.structural_sl,
        entry_change_pct=round(entry_change_pct, 8),
    )
    st.replaces += 1
    st.last_v2d_selection = refreshed
    st.repricing_audits.append(
        {
            "bar_utc": bar,
            "lineage_id": refreshed.lineage_id,
            "old_limit": old_limit,
            "new_limit": refreshed.entry_limit,
            "entry_change_pct": round(entry_change_pct, 8),
            "risk_pct": rp,
            "tp_gap_pct": round(tp_gap_pct, 8) if tp_gap_pct is not None else None,
            "old_order_id": old_order_id,
            "new_order_id": st.pending.order_id,
        }
    )
    record_structure_change(
        st.structure_audits,
        bar=bar,
        phase="PENDING_REPRICE",
        old_key=old_pool_key,
        new_key=refreshed.region_key,
        lineage_id=refreshed.lineage_id,
        old_height_pct=None,
        new_height_pct=region_pool_height_pct(cur_region),
        touch_phase=None,
        pending_order_id=st.pending.order_id,
        decision="REPRICE",
        reason="DYNAMIC_UPPER_GROWTH_V2K",
        entry_change_pct=round(entry_change_pct, 8),
    )

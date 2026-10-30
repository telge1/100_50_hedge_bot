"""Long entry chronology with Phase 2D config (support / SL / recovery / risk cap)."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
from typing import Any
from uuid import uuid4

from buy_limit_fill_v1 import evaluate_long_buy_limit_fill
from contracts_v1 import MIN_QUALIFIED_LOWER_GAP_PCT
from long_ema_gate_v1 import evaluate_long_ema_gate
from long_entry_chronology_v1 import LongEntryStateV1, _ev, _try_fill_pending
from long_dynamic_buy_limit_v2k_v1 import try_dynamic_upper_growth_reprice
from long_lineage_lifecycle_v2j_v1 import lineage_region_active_at_close, record_structure_change
from long_pending_v1 import LongPendingOrderV1, next_5m_open_after_close
from long_pool_identity_v1 import lower_pool_still_active
from long_research_config_v2d_v1 import LongResearchConfigV2DV1
from long_risk_audit_v1 import risk_pct, sl_active_interval_overlaps
from long_support_selection_v2d_v1 import (
    SelectedSupportV2DV1,
    select_support_v2d,
    validate_frozen_support_v2d,
    v2d_as_v1_compat,
)
from long_touch_recovery_v2d_v1 import process_touch_recovery_v2d_at_close
from pool_history_gate_v1 import evaluate_pool_history_gate


@dataclass
class LongEntryStateV2DV1(LongEntryStateV1):
    last_v2d_selection: SelectedSupportV2DV1 | None = None
    structure_audits: list[dict[str, Any]] = field(default_factory=list)
    repricing_audits: list[dict[str, Any]] = field(default_factory=list)


def _origin_ids_from_support(support: SelectedSupportV2DV1) -> str:
    return "|".join(support.region.distinct_origin_ids)


def _cancel_pending(st: LongEntryStateV2DV1, bar: str, reason: str, **detail: Any) -> None:
    if st.pending is None:
        return
    _ev(st, "CANCEL", bar, reason=reason, order_id=st.pending.order_id, **detail)
    st.pending = None
    st.cancels += 1


def _risk_blocked(entry: float, sl: float, config: LongResearchConfigV2DV1) -> tuple[bool, float | None]:
    rp = risk_pct(entry, sl)
    if config.max_risk_pct is None or rp is None:
        return False, rp
    if rp > config.max_risk_pct + 1e-12:
        return True, rp
    return False, rp


def process_long_entry_bar_v2d(
    st: LongEntryStateV2DV1,
    *,
    bar: str,
    bar_index: int,
    prev_bar: str | None,
    ohlc: dict[str, float],
    close_prev: float | None,
    snap_prev: dict[str, Any] | None,
    snap: dict[str, Any],
    ema9: float | None,
    ema20: float | None,
    ema59: float | None,
    ema200: float | None,
    ema200_prev6: float | None,
    config: LongResearchConfigV2DV1,
    persistence_streak: dict[str, int] | None = None,
    session: Any | None = None,
    prices: dict[str, dict[str, float]] | None = None,
    streak_cache: dict | None = None,
    closes: list[str] | None = None,
    fill_model: str = "causal_buy_limit_v2",
    lineage_tracker: Any | None = None,
    exit_variant: str = "LONG_A",
) -> LongEntryStateV2DV1:
    hist = evaluate_pool_history_gate(close_index=bar_index)
    ema = evaluate_long_ema_gate(
        ema9=ema9,
        ema20=ema20,
        ema59=ema59,
        ema200=ema200,
        ema200_prev6=ema200_prev6,
    )

    _try_fill_pending(st, bar=bar, ohlc=ohlc, snap=snap, fill_model=fill_model)
    if st.position is not None:
        return st

    if st.pending is not None and not ema.authorized:
        _cancel_pending(st, bar, "EMA_PERMISSION_LOST", ema_reason=ema.reason_code)
        return st

    if st.pending is not None:
        close = float(ohlc["c"])
        if close < st.pending.pool_lower - 1e-12:
            _cancel_pending(st, bar, "STRUCTURAL_BREAK_BELOW_SUPPORT")
            return st
        if (
            config.touch_lifecycle == "LINEAGE_AWARE_V2J"
            and st.pending.lineage_id
            and config.v2e_yaml is not None
            and session is not None
        ):
            ok_lin, lin_reason, cur_region = lineage_region_active_at_close(
                session,
                decision_close_utc=bar,
                lineage_id=st.pending.lineage_id,
                frozen_origin_ids=st.pending.origin_ids,
                lineage_tracker=lineage_tracker,
                yaml=config.v2e_yaml,
            )
            if not ok_lin:
                _cancel_pending(st, bar, "SUPPORT_DEACTIVATED", detail=lin_reason)
                return st
            if cur_region is not None and st.pending.frozen_support_key:
                new_key = f"{cur_region.lower_edge}|{cur_region.upper_edge}"
                if new_key != st.pending.frozen_support_key:
                    if config.buy_limit_semantics == "FROZEN_AT_PLACE_V2J":
                        record_structure_change(
                            st.structure_audits,
                            bar=bar,
                            phase="PENDING",
                            old_key=st.pending.frozen_support_key,
                            new_key=new_key,
                            lineage_id=st.pending.lineage_id,
                            old_height_pct=None,
                            new_height_pct=None,
                            touch_phase=None,
                            pending_order_id=st.pending.order_id,
                            decision="CONTINUE",
                            reason="PENDING_FROZEN_PRICES_HELD",
                            planned_entry=st.pending.planned_entry,
                            planned_sl=st.pending.planned_sl,
                        )
        elif not lower_pool_still_active(
            pool_lo=st.pending.pool_lower,
            pool_hi=st.pending.pool_upper,
            origin_ids=st.pending.origin_ids,
            snapshot=snap,
        ):
            _cancel_pending(st, bar, "SUPPORT_DEACTIVATED")
            return st
        ok, gap_reason = validate_frozen_support_v2d(
            snap,
            region_lower=st.pending.pool_lower,
            region_upper=st.pending.pool_upper,
            config=config,
            gap_threshold_pct=MIN_QUALIFIED_LOWER_GAP_PCT,
            persistence_bars_by_pool_key=persistence_streak,
            session=session,
            decision_close_utc=bar,
            market_close=float(ohlc["c"]),
            prices=prices,
            streak_cache=streak_cache,
            bar_index=bar_index,
            closes=closes,
            pool_age_semantics=config.pool_age_semantics,
            lineage_tracker=lineage_tracker,
            formation_semantics=config.formation_semantics,
            frozen_lineage_id=st.pending.lineage_id,
            touch_lifecycle=config.touch_lifecycle,
        )
        if not ok:
            _cancel_pending(st, bar, "GAP_OR_SUPPORT_LOST", detail=gap_reason)

        try_dynamic_upper_growth_reprice(
            st,
            bar=bar,
            close=close,
            snap=snap,
            config=config,
            session=session,
            lineage_tracker=lineage_tracker,
            streak_cache=streak_cache,
            bar_index=bar_index,
            closes=closes,
            prices=prices,
            exit_variant=exit_variant,
            ev_fn=_ev,
            risk_blocked_fn=_risk_blocked,
            origin_ids_from_support=_origin_ids_from_support,
        )

    if st.pending is not None and st.position is None:
        close = float(ohlc["c"])
        newer, _ = select_support_v2d(
            snap,
            market_price=close,
            config=config,
            persistence_bars_by_pool_key=persistence_streak,
            session=session,
            decision_close_utc=bar,
            prices=prices,
            streak_cache=streak_cache,
            bar_index=bar_index,
            closes=closes,
            pool_age_semantics=config.pool_age_semantics,
            lineage_tracker=lineage_tracker,
            formation_semantics=config.formation_semantics,
        )
        same_lineage = (
            config.touch_lifecycle == "LINEAGE_AWARE_V2J"
            and st.pending.lineage_id
            and newer is not None
            and newer.lineage_id == st.pending.lineage_id
        )
        if newer is not None and not same_lineage and newer.region_key != st.pending.pool_key:
            if newer.region.upper_edge > st.pending.pool_upper + 1e-12:
                blocked, rp = _risk_blocked(newer.entry_limit, newer.structural_sl, config)
                if blocked:
                    _ev(st, "REPLACE_SKIPPED_RISK_CAP", bar, risk_pct=rp, cap=config.max_risk_pct)
                else:
                    _ev(
                        st,
                        "REPLACE",
                        bar,
                        old_pool=st.pending.pool_key,
                        new_pool=newer.region_key,
                        new_limit=newer.entry_limit,
                        new_sl=newer.structural_sl,
                        sl_pool=newer.sl_region_key,
                    )
                    st.pending = LongPendingOrderV1(
                        order_id=str(uuid4()),
                        decision_close_utc=bar,
                        earliest_eligible_execution_utc=next_5m_open_after_close(bar),
                        pool_lower=newer.region.lower_edge,
                        pool_upper=newer.region.upper_edge,
                        pool_key=newer.region_key,
                        origin_ids=_origin_ids_from_support(newer),
                        planned_entry=newer.entry_limit,
                        planned_sl=newer.structural_sl,
                        selection_reason="REPLACE_NEW_AUTHORIZED_CANDIDATE",
                        lineage_id=newer.lineage_id,
                        frozen_support_key=newer.region_key,
                    )
                    st.last_v2d_selection = newer
                    st.replaces += 1

    tr, recovered_compat = process_touch_recovery_v2d_at_close(
        bar=bar,
        prev_bar=prev_bar,
        snap_prev=snap_prev,
        snap_now=snap,
        close_prev=close_prev,
        ohlc=ohlc,
        ema_authorized=ema.authorized and hist.ok,
        pool_history_ok=hist.ok,
        tracker=st.touch_tracker,
        config=config,
        select_fn=partial(
            select_support_v2d,
            pool_age_semantics=config.pool_age_semantics,
            lineage_tracker=lineage_tracker,
            formation_semantics=config.formation_semantics,
        ),
        validate_fn=partial(
            validate_frozen_support_v2d,
            pool_age_semantics=config.pool_age_semantics,
            lineage_tracker=lineage_tracker,
            formation_semantics=config.formation_semantics,
        ),
        persistence_streak=persistence_streak,
        session=session,
        prices=prices,
        streak_cache=streak_cache,
        bar_index=bar_index,
        closes=closes,
        lineage_tracker=lineage_tracker,
        structure_audits=st.structure_audits,
    )
    st.touch_tracker = tr

    if recovered_compat is not None and st.pending is None and st.position is None:
        recovered = st.touch_tracker.frozen_v2d
        if recovered is None:
            sel2, _ = select_support_v2d(
                snap,
                market_price=float(ohlc["c"]),
                config=config,
                persistence_bars_by_pool_key=persistence_streak,
                session=session,
                decision_close_utc=bar,
                prices=prices,
                streak_cache=streak_cache,
                bar_index=bar_index,
                closes=closes,
                pool_age_semantics=config.pool_age_semantics,
                lineage_tracker=lineage_tracker,
            )
            if sel2 is not None and sel2.region_key == recovered_compat.region_key:
                recovered = sel2
        if recovered is None:
            _ev(st, "PLACE_BLOCKED", bar, reason="V2D_SELECTION_MISMATCH")
            return st

        blocked, rp = _risk_blocked(recovered.entry_limit, recovered.structural_sl, config)
        if blocked:
            _ev(st, "PLACE_SKIPPED_RISK_CAP", bar, risk_pct=rp, cap=config.max_risk_pct)
            st.touch_tracker.phase = "IDLE"
            return st
        if recovered.structural_sl >= recovered.entry_limit - 1e-12:
            _ev(st, "PLACE_BLOCKED", bar, reason="SL_NOT_BELOW_ENTRY")
            st.touch_tracker.phase = "IDLE"
            return st

        overlap = sl_active_interval_overlaps(snap, recovered.structural_sl)
        st.pending = LongPendingOrderV1(
            order_id=str(uuid4()),
            decision_close_utc=bar,
            earliest_eligible_execution_utc=next_5m_open_after_close(bar),
            pool_lower=recovered.region.lower_edge,
            pool_upper=recovered.region.upper_edge,
            pool_key=recovered.region_key,
            origin_ids=_origin_ids_from_support(recovered),
            planned_entry=recovered.entry_limit,
            planned_sl=recovered.structural_sl,
            selection_reason="TOUCH_RECOVERY_CONFIRMED",
            lineage_id=recovered.lineage_id,
            frozen_support_key=recovered.region_key,
        )
        _ev(
            st,
            "PLACE",
            bar,
            limit=recovered.entry_limit,
            sl=recovered.structural_sl,
            sl_pool=recovered.sl_region_key,
            gap_pct=recovered.qualified_gap_pct,
            risk_pct=rp,
            sl_overlap=overlap.overlapping,
            earliest=st.pending.earliest_eligible_execution_utc,
            order_id=st.pending.order_id,
            lineage_id=recovered.lineage_id,
            frozen_support_key=recovered.region_key,
            entry_support=recovered.region.to_audit_dict(),
            sl_pool_audit=recovered.sl_region.to_audit_dict(),
            strength=recovered.strength_audit,
            skipped_pools=list(recovered.skipped_intermediate_pools),
            config=config.label(),
        )
        st.last_v2d_selection = recovered
        st.touch_tracker.phase = "IDLE"
        st.touch_tracker.frozen_support = None
        st.touch_tracker.frozen_v2d = None
        st.touch_tracker.touch_bar = None

    if not hist.ok and st.pending is None:
        _ev(st, "POOL_HISTORY_BLOCKED", bar, reason=hist.reason_code)

    return st

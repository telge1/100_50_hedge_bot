"""Single-bar long entry chronology (fill → EMA → structure → touch → pending)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from buy_limit_fill_v1 import evaluate_long_buy_limit_fill
from contracts_v1 import MIN_QUALIFIED_LOWER_GAP_PCT
from long_ema_gate_v1 import LongEmaGateResultV1, evaluate_long_ema_gate
from long_pending_v1 import LongPendingOrderV1, LongPositionV1, next_5m_open_after_close
from long_pool_identity_v1 import lower_pool_still_active
from long_risk_audit_v1 import risk_pct, sl_active_interval_overlaps, structural_long_sl
from long_support_selection_v1 import select_support_below_price, support_still_valid
from long_touch_recovery_v1 import TouchRecoveryTrackerV1, process_touch_recovery_at_close
from pool_history_gate_v1 import evaluate_pool_history_gate


@dataclass
class LongEntryStateV1:
    pending: LongPendingOrderV1 | None = None
    position: LongPositionV1 | None = None
    touch_tracker: TouchRecoveryTrackerV1 = field(default_factory=TouchRecoveryTrackerV1)
    events: list[dict[str, Any]] = field(default_factory=list)
    fills: int = 0
    cancels: int = 0
    replaces: int = 0


def _ev(st: LongEntryStateV1, action: str, bar: str, **detail: Any) -> None:
    st.events.append({"action": action, "bar_utc": bar, **detail})


def _origin_ids_from_support(support) -> str:
    return "|".join(support.region.distinct_origin_ids)


def _cancel_pending(st: LongEntryStateV1, bar: str, reason: str, **detail: Any) -> None:
    if st.pending is None:
        return
    _ev(st, "CANCEL", bar, reason=reason, order_id=st.pending.order_id, **detail)
    st.pending = None
    st.cancels += 1


def _try_fill_pending(
    st: LongEntryStateV1,
    *,
    bar: str,
    ohlc: dict[str, float],
    snap: dict[str, Any],
    fill_model: str,
) -> None:
    if st.pending is None or st.position is not None:
        return
    dec = evaluate_long_buy_limit_fill(
        fill_model=fill_model,
        limit_price=st.pending.planned_entry,
        bar_utc=bar,
        order_active_from_utc=st.pending.earliest_eligible_execution_utc,
        ohlc=ohlc,
    )
    if dec.ambiguous and not dec.filled:
        _ev(st, "FILL_AMBIGUOUS", bar, rule_id=dec.rule_id, note=dec.note)
        return
    if not dec.filled or dec.fill_price is None:
        return
    entry = float(dec.fill_price)
    sl = st.pending.planned_sl
    rp = risk_pct(entry, sl)
    overlap = sl_active_interval_overlaps(snap, sl)
    pid = str(uuid4())
    st.position = LongPositionV1(
        position_id=pid,
        entry_price=entry,
        sl_price=sl,
        fill_bar_utc=bar,
        signal_close_utc=st.pending.decision_close_utc,
        pool_key=st.pending.pool_key,
        risk_pct=round(rp, 8) if rp is not None else None,
    )
    _ev(
        st,
        "FILL",
        bar,
        entry=entry,
        sl=sl,
        risk_pct=rp,
        rule_id=dec.rule_id,
        sl_overlap=overlap.overlapping,
        order_id=st.pending.order_id,
    )
    st.pending = None
    st.fills += 1


def process_long_entry_bar(
    st: LongEntryStateV1,
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
    fill_model: str = "causal_buy_limit_v2",
) -> LongEntryStateV1:
    hist = evaluate_pool_history_gate(close_index=bar_index)
    ema: LongEmaGateResultV1 = evaluate_long_ema_gate(
        ema9=ema9,
        ema20=ema20,
        ema59=ema59,
        ema200=ema200,
        ema200_prev6=ema200_prev6,
    )

    # 1) Fill before close-side cancels (same bar)
    _try_fill_pending(st, bar=bar, ohlc=ohlc, snap=snap, fill_model=fill_model)

    if st.position is not None:
        return st

    # 2) EMA permission cancel at close
    if st.pending is not None and not ema.authorized:
        _cancel_pending(st, bar, "EMA_PERMISSION_LOST", ema_reason=ema.reason_code)
        return st

    # 3) Structural / pool / gap cancel at close
    if st.pending is not None:
        close = float(ohlc["c"])
        if close < st.pending.pool_lower - 1e-12:
            _cancel_pending(st, bar, "STRUCTURAL_BREAK_BELOW_SUPPORT")
            return st
        if not lower_pool_still_active(
            pool_lo=st.pending.pool_lower,
            pool_hi=st.pending.pool_upper,
            origin_ids=st.pending.origin_ids,
            snapshot=snap,
        ):
            _cancel_pending(st, bar, "SUPPORT_DEACTIVATED")
            return st
        ok, gap_reason = support_still_valid(
            snap,
            region_lower=st.pending.pool_lower,
            region_upper=st.pending.pool_upper,
            gap_threshold_pct=MIN_QUALIFIED_LOWER_GAP_PCT,
        )
        if not ok:
            _cancel_pending(st, bar, "GAP_OR_SUPPORT_LOST", detail=gap_reason)

    if st.pending is not None and st.position is None:
        # 4) Replace with strictly better causally valid support (same close)
        close = float(ohlc["c"])
        newer, _ = select_support_below_price(snap, market_price=close)
        if newer is not None and newer.region_key != st.pending.pool_key:
            if newer.region.upper_edge > st.pending.pool_upper + 1e-12:
                _ev(
                    st,
                    "REPLACE",
                    bar,
                    old_pool=st.pending.pool_key,
                    new_pool=newer.region_key,
                    new_limit=newer.entry_limit,
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
                )
                st.replaces += 1

    # 5) Touch / recovery (causal)
    tr, recovered = process_touch_recovery_at_close(
        bar=bar,
        prev_bar=prev_bar,
        snap_prev=snap_prev,
        snap_now=snap,
        close_prev=close_prev,
        ohlc=ohlc,
        ema_authorized=ema.authorized and hist.ok,
        pool_history_ok=hist.ok,
        tracker=st.touch_tracker,
    )
    st.touch_tracker = tr

    # 6) Authorize new pending after recovery (not if already pending or positioned)
    if recovered is not None and st.pending is None and st.position is None:
        rp = risk_pct(recovered.entry_limit, recovered.structural_sl)
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
        )
        _ev(
            st,
            "PLACE",
            bar,
            limit=recovered.entry_limit,
            sl=recovered.structural_sl,
            risk_pct=rp,
            sl_overlap=overlap.overlapping,
            earliest=st.pending.earliest_eligible_execution_utc,
        )
        st.touch_tracker.phase = "IDLE"
        st.touch_tracker.frozen_support = None
        st.touch_tracker.touch_bar = None

    if not hist.ok and st.pending is None:
        _ev(st, "POOL_HISTORY_BLOCKED", bar, reason=hist.reason_code)

    return st

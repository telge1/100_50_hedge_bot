"""Long exit order lifecycle — LONG A fixed TP and LONG B gap+EMA (isolated)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from bearish_ema_cross_exit_v1 import bearish_cross_confirmed_on_close
from long_intrabar_exit_v1 import long_intrabar_exit_decision
from upper_pool_gap_tp_v1 import select_upper_pool_gap_tp_candidate

LongExitReason = Literal[
    "STRUCTURAL_SL",
    "FIXED_UPPER_TP",
    "UPPER_POOL_GAP_TP",
    "EMA59_200_CROSS",
    "AMBIGUOUS_INTRABAR",
]


@dataclass
class LongExitStateV1:
    entry_price: float
    sl_price: float
    exit_mode: Literal["LONG_A", "LONG_B"]
    active_tp_limit: float | None = None
    active_tp_audit: dict[str, Any] | None = None
    pending_tp_limit: float | None = None
    pending_tp_audit: dict[str, Any] | None = None
    pending_clear_tp: bool = False
    pending_ema_market_on_bar: str | None = None
    pending_ema_signal: dict[str, Any] | None = None
    closed: bool = False
    exit_reason: LongExitReason | None = None
    exit_price: float | None = None
    exit_bar_utc: str | None = None
    ambiguous_intrabar: bool = False
    sl_first_shadow: dict[str, Any] | None = None


@dataclass
class LongExitAuditEventV1:
    bar_utc: str
    action: str
    detail: dict[str, Any] = field(default_factory=dict)


def _log(events: list[LongExitAuditEventV1], bar: str, action: str, **detail: Any) -> None:
    events.append(LongExitAuditEventV1(bar_utc=bar, action=action, detail=dict(detail)))


def apply_pending_tp_at_bar_open(state: LongExitStateV1, bar_utc: str, audit: list[LongExitAuditEventV1]) -> None:
    if state.closed:
        return
    if state.pending_clear_tp:
        state.pending_clear_tp = False
        if state.active_tp_limit is not None:
            _log(audit, bar_utc, "TP_LIMIT_CANCEL", price=state.active_tp_limit)
        state.active_tp_limit = None
        state.active_tp_audit = None
    if state.pending_tp_limit is None:
        return
    old = state.active_tp_limit
    state.active_tp_limit = state.pending_tp_limit
    state.active_tp_audit = state.pending_tp_audit
    state.pending_tp_limit = None
    state.pending_tp_audit = None
    if old != state.active_tp_limit:
        _log(audit, bar_utc, "TP_LIMIT_REPLACE" if old else "TP_LIMIT_PLACE", old=old, new=state.active_tp_limit)


def process_open_executable_limits(
    state: LongExitStateV1,
    *,
    bar_utc: str,
    open_price: float,
    intrabar_policy: str,
    emit_sl_first_shadow: bool,
    audit: list[LongExitAuditEventV1],
) -> None:
    if state.closed:
        return
    o = float(open_price)
    tp = state.active_tp_limit
    hit_sl = o <= state.sl_price
    hit_tp = tp is not None and o >= tp - 1e-12
    if hit_sl and hit_tp:
        state.closed = True
        state.exit_reason = "AMBIGUOUS_INTRABAR"
        state.exit_price = None
        state.exit_bar_utc = bar_utc
        state.ambiguous_intrabar = True
        _log(audit, bar_utc, "AMBIGUOUS_OPEN_GAP_SL_TP", sl=state.sl_price, tp=tp, open=o, policy=intrabar_policy)
        if emit_sl_first_shadow:
            state.sl_first_shadow = {"shadow_reason": "STRUCTURAL_SL", "shadow_price": o, "shadow_bar_utc": bar_utc}
        return
    if hit_sl:
        state.closed = True
        state.exit_reason = "STRUCTURAL_SL"
        state.exit_price = o
        state.exit_bar_utc = bar_utc
        _log(audit, bar_utc, "SL_GAP_AT_OPEN", open=o, sl=state.sl_price)
        return
    if hit_tp and tp is not None:
        state.closed = True
        state.exit_reason = "UPPER_POOL_GAP_TP" if state.exit_mode == "LONG_B" else "FIXED_UPPER_TP"
        state.exit_price = tp
        state.exit_bar_utc = bar_utc
        _log(audit, bar_utc, "TP_GAP_AT_OPEN", open=o, fill_price=tp)


def process_open_ema_market(
    state: LongExitStateV1,
    *,
    bar_utc: str,
    open_price: float,
    audit: list[LongExitAuditEventV1],
) -> bool:
    if state.closed or state.exit_mode != "LONG_B":
        return False
    if state.pending_ema_market_on_bar != bar_utc:
        return False
    state.pending_ema_market_on_bar = None
    state.active_tp_limit = None
    state.pending_tp_limit = None
    _log(audit, bar_utc, "EMA_MARKET_EXIT", open_price=open_price, signal=state.pending_ema_signal)
    state.pending_ema_signal = None
    state.closed = True
    state.exit_reason = "EMA59_200_CROSS"
    state.exit_price = open_price
    state.exit_bar_utc = bar_utc
    return True


def process_intrabar_limits(
    state: LongExitStateV1,
    *,
    bar_utc: str,
    ohlc: dict[str, float],
    intrabar_policy: str,
    emit_sl_first_shadow: bool,
    audit: list[LongExitAuditEventV1],
) -> None:
    if state.closed:
        return
    tp = state.active_tp_limit
    if tp is None:
        if float(ohlc["l"]) <= state.sl_price:
            state.closed = True
            state.exit_reason = "STRUCTURAL_SL"
            state.exit_price = state.sl_price
            state.exit_bar_utc = bar_utc
            _log(audit, bar_utc, "SL_FILL", price=state.sl_price)
        return
    reason, price, ambiguous = long_intrabar_exit_decision(
        ohlc=ohlc, sl_price=state.sl_price, tp_price=tp, policy=intrabar_policy
    )
    if reason is None:
        return
    if reason == "AMBIGUOUS_INTRABAR":
        state.closed = True
        state.exit_reason = "AMBIGUOUS_INTRABAR"
        state.exit_price = None
        state.exit_bar_utc = bar_utc
        state.ambiguous_intrabar = True
        _log(audit, bar_utc, "AMBIGUOUS_SL_TP_SAME_BAR", sl=state.sl_price, tp=tp)
        if emit_sl_first_shadow:
            state.sl_first_shadow = {"shadow_reason": "STRUCTURAL_SL", "shadow_price": state.sl_price}
        return
    if reason == "SL":
        state.closed = True
        state.exit_reason = "STRUCTURAL_SL"
        state.exit_price = price
        state.exit_bar_utc = bar_utc
        _log(audit, bar_utc, "SL_FILL", price=price)
        return
    state.closed = True
    state.exit_reason = "UPPER_POOL_GAP_TP" if state.exit_mode == "LONG_B" else "FIXED_UPPER_TP"
    state.exit_price = price
    state.exit_bar_utc = bar_utc
    _log(audit, bar_utc, "TP_LIMIT_FILL", price=price)


def process_close_rebalance_long_b(
    state: LongExitStateV1,
    *,
    prev_bar_utc: str,
    bar_utc: str,
    next_bar_utc: str | None,
    close_price: float,
    snapshot: dict[str, Any],
    prev_ema59: float,
    prev_ema200: float,
    ema59: float,
    ema200: float,
    gap_threshold_pct: float,
    min_unique_origins: int,
    audit: list[LongExitAuditEventV1],
) -> None:
    if state.closed or state.exit_mode != "LONG_B":
        return
    sig = bearish_cross_confirmed_on_close(
        prev_bar_utc=prev_bar_utc,
        bar_utc=bar_utc,
        prev_ema_fast=prev_ema59,
        prev_ema_slow=prev_ema200,
        ema_fast=ema59,
        ema_slow=ema200,
    )
    if sig and next_bar_utc:
        state.pending_ema_market_on_bar = next_bar_utc
        state.pending_ema_signal = {"signal_close_bar": bar_utc, "execute_open_bar": next_bar_utc}
        _log(audit, bar_utc, "EMA_CROSS_ARMED", execute_on=next_bar_utc)

    cand = select_upper_pool_gap_tp_candidate(
        snapshot,
        market_price=close_price,
        entry_fill=state.entry_price,
        gap_threshold_pct=gap_threshold_pct,
        min_unique_origins=min_unique_origins,
    )
    if cand is None:
        state.pending_clear_tp = True
        state.pending_tp_limit = None
        return
    new_px = float(cand["tp_limit_price"])
    if new_px <= state.entry_price + 1e-12:
        state.pending_clear_tp = True
        return
    state.pending_tp_limit = new_px
    state.pending_tp_audit = cand
    state.pending_clear_tp = False
    _log(audit, bar_utc, "TP_CANDIDATE_SELECTED", effective_bar=next_bar_utc, tp_limit_price=new_px, candidate=cand)


def process_long_exit_bar(
    state: LongExitStateV1,
    *,
    prev_bar_utc: str,
    bar_utc: str,
    next_bar_utc: str | None,
    ohlc: dict[str, float],
    snapshot_at_close: dict[str, Any],
    prev_ema59: float,
    prev_ema200: float,
    ema59: float,
    ema200: float,
    intrabar_policy: str = "no_trade_ambiguous",
    emit_sl_first_shadow: bool = True,
    gap_threshold_pct: float = 3.5,
    min_unique_origins: int = 2,
    audit: list[LongExitAuditEventV1],
) -> None:
    if state.exit_mode == "LONG_B":
        apply_pending_tp_at_bar_open(state, bar_utc, audit)
    elif state.pending_tp_limit is not None and state.active_tp_limit is None:
        apply_pending_tp_at_bar_open(state, bar_utc, audit)
    process_open_executable_limits(
        state,
        bar_utc=bar_utc,
        open_price=float(ohlc["o"]),
        intrabar_policy=intrabar_policy,
        emit_sl_first_shadow=emit_sl_first_shadow,
        audit=audit,
    )
    if state.closed:
        return
    if process_open_ema_market(state, bar_utc=bar_utc, open_price=float(ohlc["o"]), audit=audit):
        return
    process_intrabar_limits(
        state,
        bar_utc=bar_utc,
        ohlc=ohlc,
        intrabar_policy=intrabar_policy,
        emit_sl_first_shadow=emit_sl_first_shadow,
        audit=audit,
    )
    if state.closed:
        return
    if state.exit_mode == "LONG_B":
        process_close_rebalance_long_b(
            state,
            prev_bar_utc=prev_bar_utc,
            bar_utc=bar_utc,
            next_bar_utc=next_bar_utc,
            close_price=float(ohlc["c"]),
            snapshot=snapshot_at_close,
            prev_ema59=prev_ema59,
            prev_ema200=prev_ema200,
            ema59=ema59,
            ema200=ema200,
            gap_threshold_pct=gap_threshold_pct,
            min_unique_origins=min_unique_origins,
            audit=audit,
        )

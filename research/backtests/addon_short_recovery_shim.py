from __future__ import annotations

"""Backtest-only simulator hook for Blocker Addon Short Recovery.

This module wires AddonShortRecoveryConfig into the backtest harness:

- tracks activation and addon-short subaccount state per trade
- decides when to open/close addon shorts and when to reduce long qty
- exposes a single per-candle entry point for run_historical_backtest

It does NOT touch live-bot code paths.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Iterable, List

from fixed_cycle_hedge_bot.models import FillEvent, StrategyIntent

from .addon_short_recovery import AddonShortRecoveryConfig, AddonShortRecoveryEvent
from .simulated_execution import virtual_order_to_fill_event
from .simulated_order_book import SimulatedOrderBook, SyntheticCandle
from .simulated_pnl import calculate_simulated_closed_pnl
from .backtest_audit_recorder import BacktestAuditRecorder, AddonAuditRecord

if TYPE_CHECKING:
    from .hedge_bot_original_simulator import HedgeBotOriginalSimulator
    from .backtest_report import BacktestResult


ADDON_SHORT_ENTRY_PURPOSE = "ADDON_RECOVERY_SHORT_ENTRY"
ADDON_SHORT_TP_PURPOSE = "ADDON_RECOVERY_SHORT_TP"
ADDON_SHORT_REBOUND_PURPOSE = "ADDON_RECOVERY_SHORT_REBOUND_EXIT"
ADDON_SHORT_HARD_STOP_PURPOSE = "ADDON_RECOVERY_SHORT_HARD_STOP"
ADDON_LONG_REDUCE_PURPOSE = "ADDON_RECOVERY_LONG_REDUCE"


@dataclass
class AddonShortRecoveryState:
    activated: bool = False
    activation_candle_index: int | None = None
    activation_timestamp: str | None = None
    activation_price: float | None = None
    long_qty_at_activation: float | None = None
    normal_short_qty_at_activation: float | None = None
    long_avg_price_at_activation: float | None = None
    recovery_gap_at_activation: float | None = None
    addon_short_step_qty: float | None = None

    # Live references at last update (for analysis/export)
    last_long_qty: float | None = None
    last_short_qty: float | None = None

    # Subaccount short
    has_open_addon_short: bool = False
    addon_short_entry_price: float | None = None
    addon_short_entry_timestamp: str | None = None
    addon_short_entry_candle_index: int | None = None
    addon_short_qty_open: float | None = None
    lowest_price_since_entry: float | None = None
    maximum_favorable_move_pct: float | None = None

    # Reentry state
    previous_low: float | None = None
    previous_tp_price: float | None = None
    previous_entry_price: float | None = None
    cooldown_after_close: bool = False

    # Aggregated PnL / stats
    addon_short_realized_profit: float = 0.0
    addon_short_realized_loss: float = 0.0
    addon_short_tp_count: int = 0
    addon_short_rebound_exit_count: int = 0
    addon_short_hard_stop_count: int = 0
    long_reduce_total_qty: float = 0.0
    long_reduce_total_pnl: float = 0.0

    # Completion
    recovery_completed: bool = False
    recovery_completion_reason: str = ""
    recovery_completed_candle_index: int | None = None

    # Internal counters
    addon_short_trade_count: int = 0

    # Backtest-only: last close price/candle for audit linkage
    last_addon_close_price: float | None = None
    last_addon_close_candle_index: int | None = None
    last_addon_close_audit_event_sequence: int | None = None
    last_addon_close_audit_event_sequence_in_candle: int | None = None
    last_addon_close_trade_id: int | None = None


@dataclass
class AddonShortRecoveryTracker:
    config: AddonShortRecoveryConfig
    state: AddonShortRecoveryState = field(default_factory=AddonShortRecoveryState)
    events: list[AddonShortRecoveryEvent] = field(default_factory=list)


def attach_addon_short_recovery_tracker(
    sim: "HedgeBotOriginalSimulator",
    config: AddonShortRecoveryConfig | None,
) -> AddonShortRecoveryTracker | None:
    """Attach tracker to simulator if config is enabled."""
    if config is None or not config.enabled:
        return None
    tracker = AddonShortRecoveryTracker(config=config)
    # For debug/inspection only; not used by runtime logic.
    setattr(sim, "addon_short_recovery_tracker", tracker)
    return tracker


def _now_iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _remaining_gap(long_qty: float, normal_short_qty: float) -> float:
    return max(0.0, float(long_qty) - float(normal_short_qty))


def _maybe_activate_on_fills(
    *,
    sim: "HedgeBotOriginalSimulator",
    tracker: AddonShortRecoveryTracker,
    fills: Iterable[FillEvent],
    candle: SyntheticCandle,
    candle_index: int,
) -> None:
    if tracker is None or tracker.state.activated:
        return
    cfg = tracker.config
    activation_purpose = str(cfg.activation_order or "").strip().upper()
    for fill in fills:
        purpose = str(fill.purpose or "").strip().upper()
        if purpose != activation_purpose:
            continue
        state = tracker.state
        before = _snapshot_addon_state(state)
        state.activated = True
        state.activation_candle_index = candle_index
        state.activation_timestamp = _now_iso(fill.occurred_at)
        state.activation_price = float(fill.exec_price)
        state.long_qty_at_activation = float(sim.book.long_qty or 0.0)
        state.normal_short_qty_at_activation = float(sim.book.short_qty or 0.0)
        state.long_avg_price_at_activation = float(sim.book.long_avg or 0.0)
        state.recovery_gap_at_activation = max(
            0.0,
            float(state.long_qty_at_activation) - float(state.normal_short_qty_at_activation),
        )
        state.addon_short_step_qty = float(
            (state.recovery_gap_at_activation or 0.0) * float(cfg.addon_short_step_fraction)
        )
        state.last_long_qty = state.long_qty_at_activation
        state.last_short_qty = state.normal_short_qty_at_activation
        after = _snapshot_addon_state(state)
        _record_addon_event(
            sim=sim,
            tracker=tracker,
            event_type="RECOVERY_ACTIVATED",
            event_reason="activation_order_fill",
            candle=candle,
            candle_index=candle_index,
            before=before,
            after=after,
            extra=None,
        )
        # Cancel open cycle/refill orders except exits if requested.
        if cfg.cancel_open_cycle_orders:
            _cancel_cycle_and_refill_orders_except_exits(sim)
        # Install intent filter to block new cycle/refill/long-add intents.
        if cfg.stop_new_cycle_orders:
            sim.intent_filter = _build_intent_filter(cfg)
        break


def _build_intent_filter(config: AddonShortRecoveryConfig):
    """Return a predicate that blocks new cycle/refill/long-add intents."""

    def _filter(intent: StrategyIntent) -> bool:
        purpose = str(intent.purpose or "").strip().upper()
        if not purpose:
            return True
        # Exit orders should remain allowed.
        if purpose in {"LONG_TP_EXIT", "LONG_SL_EXIT", "SHORT_TP_EXIT", "SHORT_SL_EXIT"}:
            return True
        # Block all CYCLE_* except *_EXIT when recovery is active.
        if purpose.startswith("CYCLE_") and not purpose.endswith("_EXIT"):
            return False
        # Block generic refills and long-adds.
        if purpose in {"REFILL_LONG", "REFILL_SHORT", "RECOVERY_REFILL_LONG", "RECOVERY_REFILL_SHORT"}:
            return False
        if purpose.endswith("_LONG_ADD"):
            return False
        return True

    return _filter


def _cancel_cycle_and_refill_orders_except_exits(sim: "HedgeBotOriginalSimulator") -> None:
    """Cancel active cycle/refill orders while keeping existing exits."""
    book: SimulatedOrderBook = sim.book
    cancelled_any = False
    for order in list(book.active_orders()):
        purpose = str(order.purpose or "").strip().upper()
        if not purpose:
            continue
        if purpose in {"LONG_TP_EXIT", "LONG_SL_EXIT", "SHORT_TP_EXIT", "SHORT_SL_EXIT"}:
            continue
        if purpose.startswith("CYCLE_") or purpose in {
            "REFILL_LONG",
            "REFILL_SHORT",
            "RECOVERY_REFILL_LONG",
            "RECOVERY_REFILL_SHORT",
        }:
            if not book.cancel_by_order_id(order.order_id):
                continue
            cancelled_any = True
            sim._record_order_event(
                order,
                event_type="cancelled",
                status="CANCELED",
            )
    if cancelled_any:
        book.sync_runtime_state(sim.runtime_state)
        sim._refresh_snapshot_from_book(
            source="after_addon_short_recovery_cancel",
            price=sim.candle.close,
        )


def _update_state_with_book(tracker: AddonShortRecoveryTracker, book: SimulatedOrderBook) -> None:
    tracker.state.last_long_qty = float(book.long_qty or 0.0)
    tracker.state.last_short_qty = float(book.short_qty or 0.0)


def _get_audit_recorder(sim: "HedgeBotOriginalSimulator") -> BacktestAuditRecorder | None:
    recorder = getattr(sim, "audit_recorder", None)
    if recorder is None or not isinstance(recorder, BacktestAuditRecorder):
        return None
    if not recorder.enabled:
        return None
    return recorder


def _snapshot_addon_state(state: AddonShortRecoveryState) -> dict[str, Any]:
    return {
        "has_open_addon_short": bool(state.has_open_addon_short),
        "addon_short_qty_open": float(state.addon_short_qty_open or 0.0),
        "addon_short_entry_price": float(state.addon_short_entry_price or 0.0)
        if state.addon_short_entry_price is not None
        else None,
        "addon_short_trade_count": int(state.addon_short_trade_count),
        "previous_low": state.previous_low,
        "lowest_price_since_entry": state.lowest_price_since_entry,
        "maximum_favorable_move_pct": state.maximum_favorable_move_pct,
        "last_addon_close_price": state.last_addon_close_price,
        "last_addon_close_candle_index": state.last_addon_close_candle_index,
        "recovery_active": bool(state.activated),
        "recovery_activation_candle_index": state.activation_candle_index,
        "recovery_completed": bool(state.recovery_completed),
        "recovery_completed_candle_index": state.recovery_completed_candle_index,
        "addon_short_realized_profit": state.addon_short_realized_profit,
        "addon_short_realized_loss": state.addon_short_realized_loss,
        "long_reduce_total_qty": state.long_reduce_total_qty,
        "long_reduce_total_pnl": state.long_reduce_total_pnl,
    }


def record_addon_recovery_series_end(
    *,
    sim: "HedgeBotOriginalSimulator",
    tracker: AddonShortRecoveryTracker,
    result: "BacktestResult",
    last_candle: SyntheticCandle,
    last_candle_index: int,
) -> None:
    """Backtest-only helper: log final addon recovery state at series end.

    Does not mutate strategy or positions; it only emits an audit record when
    the BacktestAuditRecorder is enabled.
    """
    recorder = _get_audit_recorder(sim)
    if recorder is None:
        return

    state = tracker.state
    before = _snapshot_addon_state(state)
    after = dict(before)

    _record_addon_event(
        sim=sim,
        tracker=tracker,
        event_type="RECOVERY_SERIES_END",
        event_reason=state.recovery_completion_reason or "series_end",
        candle=last_candle,
        candle_index=last_candle_index,
        before=before,
        after=after,
        extra={
            # Interpret the final close as the last observed market price.
            "close_price": float(last_candle.close),
        },
    )


def _record_addon_event(
    *,
    sim: "HedgeBotOriginalSimulator",
    tracker: AddonShortRecoveryTracker,
    event_type: str,
    event_reason: str | None,
    candle: SyntheticCandle,
    candle_index: int,
    before: dict[str, Any],
    after: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> AddonAuditRecord | None:
    recorder = _get_audit_recorder(sim)
    if recorder is None:
        return None

    global_seq, candle_seq = recorder.next_event_sequence(candle_index)
    state = tracker.state
    book = sim.book

    rec = AddonAuditRecord(
        global_event_sequence=global_seq,
        event_sequence_in_candle=candle_seq,
        candle_index=candle_index,
        event_timestamp=candle.timestamp.isoformat() if candle.timestamp is not None else None,
        event_type=event_type,
        event_reason=event_reason,
        record_source="addon_short_recovery_shim",
        runtime_logged=True,
    )

    rec.trade_id = getattr(sim, "config", None).trade_block_id if hasattr(sim.config, "trade_block_id") else None
    rec.addon_trade_id = after.get("addon_short_trade_count")
    rec.recovery_active_before = before.get("recovery_active")
    rec.recovery_active_after = after.get("recovery_active")
    rec.recovery_activation_candle_index = state.activation_candle_index
    rec.recovery_completion_candle_index = state.recovery_completed_candle_index
    rec.recovery_completed_before = before.get("recovery_completed")
    rec.recovery_completed_after = after.get("recovery_completed")

    rec.has_open_addon_short_before = before.get("has_open_addon_short")
    rec.addon_short_qty_before = before.get("addon_short_qty_open")
    rec.addon_short_entry_price_before = before.get("addon_short_entry_price")
    rec.addon_short_avg_before = before.get("addon_short_entry_price")
    rec.addon_short_trade_count_before = before.get("addon_short_trade_count")
    rec.previous_low_before = before.get("previous_low")
    rec.lowest_price_since_entry_before = before.get("lowest_price_since_entry")
    rec.maximum_favorable_move_pct_before = before.get("maximum_favorable_move_pct")
    rec.last_addon_close_price_before = before.get("last_addon_close_price")
    rec.last_addon_close_candle_index_before = before.get("last_addon_close_candle_index")

    rec.has_open_addon_short_after = after.get("has_open_addon_short")
    rec.addon_short_qty_after = after.get("addon_short_qty_open")
    rec.addon_short_entry_price_after = after.get("addon_short_entry_price")
    rec.addon_short_avg_after = after.get("addon_short_entry_price")
    rec.addon_short_trade_count_after = after.get("addon_short_trade_count")
    rec.previous_low_after = after.get("previous_low")
    rec.lowest_price_since_entry_after = after.get("lowest_price_since_entry")
    rec.maximum_favorable_move_pct_after = after.get("maximum_favorable_move_pct")
    rec.last_addon_close_price_after = after.get("last_addon_close_price")
    rec.last_addon_close_candle_index_after = after.get("last_addon_close_candle_index")

    # Snapshot main-book state around the event. These may be overridden by
    # event-specific extras (e.g. long-reduce) when more precise pre/post
    # quantities are known.
    rec.long_qty_before = float(book.long_qty or 0.0)
    rec.long_avg_before = float(book.long_avg or 0.0)
    rec.normal_short_qty_before = float(book.short_qty or 0.0)
    rec.normal_short_avg_before = float(book.short_avg or 0.0)
    rec.long_qty_after = rec.long_qty_before
    rec.long_avg_after = rec.long_avg_before
    rec.normal_short_qty_after = rec.normal_short_qty_before
    rec.normal_short_avg_after = rec.normal_short_avg_before

    # Apply any extra fields that should override the generic snapshot. This is
    # used, for example, by ADDON_LONG_REDUCE to inject precise pre/post
    # quantities from before and after the synthetic reduce fill.
    if extra:
        for key, value in extra.items():
            if hasattr(rec, key):
                setattr(rec, key, value)

    # Derived combined-short and remaining-gap metrics around the event based on
    # the (possibly overridden) quantities.
    addon_qty_before = float(before.get("addon_short_qty_open") or 0.0)
    addon_qty_after = float(after.get("addon_short_qty_open") or 0.0)
    rec.combined_short_qty_before = float(rec.normal_short_qty_before or 0.0) + addon_qty_before
    rec.combined_short_qty_after = float(rec.normal_short_qty_after or 0.0) + addon_qty_after
    rec.remaining_gap_before = _remaining_gap(
        float(rec.long_qty_before or 0.0),
        float(rec.normal_short_qty_before or 0.0),
    )
    rec.remaining_gap_after = _remaining_gap(
        float(rec.long_qty_after or 0.0),
        float(rec.normal_short_qty_after or 0.0),
    )

    # Aggregates copied directly from snapshot state.
    rec.addon_short_realized_profit_before = before.get("addon_short_realized_profit")
    rec.addon_short_realized_profit_after = after.get("addon_short_realized_profit")
    rec.addon_short_realized_loss_before = before.get("addon_short_realized_loss")
    rec.addon_short_realized_loss_after = after.get("addon_short_realized_loss")
    rec.addon_short_net_realized_pnl_before = (
        float(rec.addon_short_realized_profit_before or 0.0)
        - float(rec.addon_short_realized_loss_before or 0.0)
        if before.get("addon_short_realized_profit") is not None
        and before.get("addon_short_realized_loss") is not None
        else None
    )
    rec.addon_short_net_realized_pnl_after = (
        float(rec.addon_short_realized_profit_after or 0.0)
        - float(rec.addon_short_realized_loss_after or 0.0)
        if after.get("addon_short_realized_profit") is not None
        and after.get("addon_short_realized_loss") is not None
        else None
    )
    rec.long_reduce_total_qty_before = before.get("long_reduce_total_qty")
    rec.long_reduce_total_qty_after = after.get("long_reduce_total_qty")
    rec.long_reduce_total_pnl_before = before.get("long_reduce_total_pnl")
    rec.long_reduce_total_pnl_after = after.get("long_reduce_total_pnl")

    recorder.record_addon_event(rec)
    return rec


def _open_addon_short_immediately_at_activation_price(
    *,
    sim: "HedgeBotOriginalSimulator",
    tracker: AddonShortRecoveryTracker,
) -> None:
    cfg = tracker.config
    state = tracker.state
    if state.has_open_addon_short:
        return
    if state.activation_price is None:
        return
    long_qty = float(sim.book.long_qty or 0.0)
    normal_short = float(sim.book.short_qty or 0.0)
    gap = _remaining_gap(long_qty, normal_short)
    if gap <= 0:
        return
    step_qty = float(state.addon_short_step_qty or 0.0)
    if step_qty <= 0:
        return
    qty = min(step_qty, gap)
    if qty <= 0:
        return
    # Net-short guard.
    if not cfg.allow_net_short and long_qty < normal_short + qty:
        qty = max(0.0, long_qty - normal_short)
        if qty <= 0:
            return
    before = _snapshot_addon_state(state)
    state.has_open_addon_short = True
    state.addon_short_qty_open = qty
    state.addon_short_entry_price = float(state.activation_price)
    state.addon_short_entry_timestamp = state.activation_timestamp
    state.addon_short_entry_candle_index = state.activation_candle_index
    state.lowest_price_since_entry = float(state.activation_price)
    state.maximum_favorable_move_pct = 0.0
    state.previous_entry_price = state.activation_price
    state.previous_low = state.activation_price
    state.addon_short_trade_count += 1
    tracker.events.append(
        AddonShortRecoveryEvent(
            event_type=ADDON_SHORT_ENTRY_PURPOSE,
            trade_index=state.addon_short_trade_count,
            entry_timestamp=state.addon_short_entry_timestamp,
            entry_candle_index=state.addon_short_entry_candle_index,
            entry_price=state.addon_short_entry_price,
            entry_qty=qty,
            activation_price=state.activation_price,
            activation_timestamp=state.activation_timestamp,
        )
    )
    after = _snapshot_addon_state(state)
    _record_addon_event(
        sim=sim,
        tracker=tracker,
        event_type="ADDON_SHORT_FIRST_ENTRY",
        event_reason="activation_immediate_entry",
        candle=sim.candle,
        candle_index=state.activation_candle_index or 0,
        before=before,
        after=after,
        extra={
            "first_entry_or_reentry": "first_entry",
            "requested_entry_qty": float(state.addon_short_step_qty or qty),
            "executed_entry_qty": qty,
            "entry_price": state.addon_short_entry_price,
            "remaining_gap_before_entry": _remaining_gap(long_qty, normal_short),
            "remaining_gap_after_entry": _remaining_gap(
                long_qty, normal_short + float(state.addon_short_qty_open or 0.0)
            ),
        },
    )


def _update_trailing_low_for_candle(
    *,
    tracker: AddonShortRecoveryTracker,
    candle: SyntheticCandle,
    use_previous_only: bool,
) -> None:
    state = tracker.state
    if not state.has_open_addon_short:
        return
    if state.addon_short_entry_price is None:
        return
    if state.lowest_price_since_entry is None:
        state.lowest_price_since_entry = state.addon_short_entry_price
    # For version 1 we use only the low up to the *previous* candle.
    # The caller passes use_previous_only=True for trailing recomputation that
    # must not include the current candle's low.
    if use_previous_only:
        return
    low = float(candle.low if candle.low is not None else candle.close)
    if low < state.lowest_price_since_entry:
        state.lowest_price_since_entry = low
    if state.lowest_price_since_entry is not None:
        move = (
            (state.addon_short_entry_price - state.lowest_price_since_entry)
            / state.addon_short_entry_price
            * 100.0
        )
        if state.maximum_favorable_move_pct is None:
            state.maximum_favorable_move_pct = move
        else:
            state.maximum_favorable_move_pct = max(state.maximum_favorable_move_pct, move)


def _compute_short_pnl_for_close(
    *,
    entry_price: float,
    close_price: float,
    qty: float,
    fee_rate: float | None,
) -> float:
    pnl, _details = calculate_simulated_closed_pnl(
        side="short",
        avg_entry_price=float(entry_price),
        fill_price=float(close_price),
        qty=float(qty),
        reduce_only=True,
        fee_rate=fee_rate,
    )
    return float(pnl)


def _maybe_close_addon_short_on_candle(
    *,
    sim: "HedgeBotOriginalSimulator",
    tracker: AddonShortRecoveryTracker,
    candle: SyntheticCandle,
    candle_index: int,
    previous_low_for_trailing: float | None,
) -> tuple[bool, float | None, str | None]:
    """Return (closed, close_price, close_reason)."""
    cfg = tracker.config
    state = tracker.state
    if not state.has_open_addon_short:
        return False, None, None
    entry_price = float(state.addon_short_entry_price or 0.0)
    qty = float(state.addon_short_qty_open or 0.0)
    if entry_price <= 0 or qty <= 0:
        return False, None, None
    low = float(candle.low if candle.low is not None else candle.close)
    high = float(candle.high if candle.high is not None else candle.close)

    # TP / Hard-Stop thresholds use full OHLC logic.
    tp_price = entry_price * (1.0 - float(cfg.addon_short_tp_pct) / 100.0)
    hard_stop_price = entry_price * (1.0 + float(cfg.addon_short_hard_stop_pct) / 100.0)

    # Trailing low for rebound uses only previous-candle low.
    trailing_low = previous_low_for_trailing
    rebound_close_price = None
    can_rebound = False
    if trailing_low is not None and trailing_low > 0:
        move_pct = (entry_price - trailing_low) / entry_price * 100.0
        if move_pct >= float(cfg.addon_short_min_favorable_move_pct):
            can_rebound = True
            rebound_close_price = trailing_low * (1.0 + float(cfg.addon_short_rebound_close_pct) / 100.0)

    # Conservative same-candle ordering: Hard-Stop > TP > Rebound.
    before = _snapshot_addon_state(state)

    # Hard-Stop.
    if high >= hard_stop_price:
        close_price = hard_stop_price
        pnl = _compute_short_pnl_for_close(
            entry_price=entry_price,
            close_price=close_price,
            qty=qty,
            fee_rate=sim.book.fee_rate,
        )
        state.has_open_addon_short = False
        state.addon_short_qty_open = None
        state.previous_low = trailing_low if trailing_low is not None else low
        tracker.state.addon_short_hard_stop_count += 1
        if pnl >= 0:
            tracker.state.addon_short_realized_profit += pnl
        else:
            tracker.state.addon_short_realized_loss += -pnl
        tracker.events.append(
            AddonShortRecoveryEvent(
                event_type=ADDON_SHORT_HARD_STOP_PURPOSE,
                trade_index=state.addon_short_trade_count,
                close_timestamp=_now_iso(candle.timestamp),
                close_candle_index=candle_index,
                close_price=close_price,
                close_qty=qty,
                previous_low=state.previous_low,
                maximum_favorable_move_pct=state.maximum_favorable_move_pct,
                gross_pnl=pnl,
                net_pnl=pnl,
            )
        )
        state.last_addon_close_price = close_price
        state.last_addon_close_candle_index = candle_index
        after = _snapshot_addon_state(state)
        rec = _record_addon_event(
            sim=sim,
            tracker=tracker,
            event_type="ADDON_SHORT_HARD_STOP_CLOSE",
            event_reason="hard_stop",
            candle=candle,
            candle_index=candle_index,
            before=before,
            after=after,
            extra={
                "requested_close_qty": qty,
                "executed_close_qty": qty,
                "close_price": close_price,
                "close_reason": "hard_stop",
                "hard_stop_price": hard_stop_price,
                "maximum_favorable_move_pct_at_close": state.maximum_favorable_move_pct,
                "gross_pnl": pnl,
                "net_pnl": pnl,
                "addon_trade_id": state.addon_short_trade_count,
            },
        )
        if rec is not None:
            state.last_addon_close_audit_event_sequence = rec.global_event_sequence
            state.last_addon_close_audit_event_sequence_in_candle = rec.event_sequence_in_candle
            state.last_addon_close_trade_id = rec.addon_trade_id
        state.cooldown_after_close = True
        return True, close_price, "hard_stop"

    # TP.
    if low <= tp_price:
        close_price = tp_price
        pnl = _compute_short_pnl_for_close(
            entry_price=entry_price,
            close_price=close_price,
            qty=qty,
            fee_rate=sim.book.fee_rate,
        )
        state.has_open_addon_short = False
        state.addon_short_qty_open = None
        state.previous_low = trailing_low if trailing_low is not None else low
        tracker.state.addon_short_tp_count += 1
        if pnl >= 0:
            tracker.state.addon_short_realized_profit += pnl
        else:
            tracker.state.addon_short_realized_loss += -pnl
        tracker.events.append(
            AddonShortRecoveryEvent(
                event_type=ADDON_SHORT_TP_PURPOSE,
                trade_index=state.addon_short_trade_count,
                close_timestamp=_now_iso(candle.timestamp),
                close_candle_index=candle_index,
                close_price=close_price,
                close_qty=qty,
                previous_low=state.previous_low,
                maximum_favorable_move_pct=state.maximum_favorable_move_pct,
                gross_pnl=pnl,
                net_pnl=pnl,
            )
        )
        state.last_addon_close_price = close_price
        state.last_addon_close_candle_index = candle_index
        after = _snapshot_addon_state(state)
        rec = _record_addon_event(
            sim=sim,
            tracker=tracker,
            event_type="ADDON_SHORT_TP_CLOSE",
            event_reason="tp",
            candle=candle,
            candle_index=candle_index,
            before=before,
            after=after,
            extra={
                "requested_close_qty": qty,
                "executed_close_qty": qty,
                "close_price": close_price,
                "close_reason": "tp",
                "tp_price": tp_price,
                "maximum_favorable_move_pct_at_close": state.maximum_favorable_move_pct,
                "gross_pnl": pnl,
                "net_pnl": pnl,
                "addon_trade_id": state.addon_short_trade_count,
            },
        )
        if rec is not None:
            state.last_addon_close_audit_event_sequence = rec.global_event_sequence
            state.last_addon_close_audit_event_sequence_in_candle = rec.event_sequence_in_candle
            state.last_addon_close_trade_id = rec.addon_trade_id
        state.cooldown_after_close = True
        return True, close_price, "tp"

    # Rebound-close.
    if can_rebound and rebound_close_price is not None and high >= rebound_close_price:
        close_price = rebound_close_price
        pnl = _compute_short_pnl_for_close(
            entry_price=entry_price,
            close_price=close_price,
            qty=qty,
            fee_rate=sim.book.fee_rate,
        )
        state.has_open_addon_short = False
        state.addon_short_qty_open = None
        state.previous_low = trailing_low
        tracker.state.addon_short_rebound_exit_count += 1
        if pnl >= 0:
            tracker.state.addon_short_realized_profit += pnl
        else:
            tracker.state.addon_short_realized_loss += -pnl
        tracker.events.append(
            AddonShortRecoveryEvent(
                event_type=ADDON_SHORT_REBOUND_PURPOSE,
                trade_index=state.addon_short_trade_count,
                close_timestamp=_now_iso(candle.timestamp),
                close_candle_index=candle_index,
                close_price=close_price,
                close_qty=qty,
                previous_low=state.previous_low,
                maximum_favorable_move_pct=state.maximum_favorable_move_pct,
                gross_pnl=pnl,
                net_pnl=pnl,
            )
        )
        state.last_addon_close_price = close_price
        state.last_addon_close_candle_index = candle_index
        after = _snapshot_addon_state(state)
        rec = _record_addon_event(
            sim=sim,
            tracker=tracker,
            event_type="ADDON_SHORT_REBOUND_CLOSE",
            event_reason="rebound",
            candle=candle,
            candle_index=candle_index,
            before=before,
            after=after,
            extra={
                "requested_close_qty": qty,
                "executed_close_qty": qty,
                "close_price": close_price,
                "close_reason": "rebound",
                "rebound_price": close_price,
                "maximum_favorable_move_pct_at_close": state.maximum_favorable_move_pct,
                "gross_pnl": pnl,
                "net_pnl": pnl,
                "addon_trade_id": state.addon_short_trade_count,
            },
        )
        if rec is not None:
            state.last_addon_close_audit_event_sequence = rec.global_event_sequence
            state.last_addon_close_audit_event_sequence_in_candle = rec.event_sequence_in_candle
            state.last_addon_close_trade_id = rec.addon_trade_id
        state.cooldown_after_close = True
        return True, close_price, "rebound"

    return False, None, None


def _maybe_long_reduce_after_tp(
    *,
    sim: "HedgeBotOriginalSimulator",
    result: "BacktestResult",
    tracker: AddonShortRecoveryTracker,
    close_price: float,
    short_trade_pnl: float,
    candle: SyntheticCandle,
    candle_index: int,
) -> None:
    cfg = tracker.config
    state = tracker.state
    # Only TP-closure uses long reduction in version 1.
    if short_trade_pnl <= 0:
        return
    usable = float(short_trade_pnl) * float(cfg.long_reduce_profit_usage_fraction)
    long_avg = float(sim.book.long_avg or 0.0)
    long_qty = float(sim.book.long_qty or 0.0)
    normal_short = float(sim.book.short_qty or 0.0)
    if long_avg <= 0 or long_qty <= 0:
        return
    long_loss_per_unit = long_avg - float(close_price)
    if long_loss_per_unit <= 0:
        return
    raw_reduce_qty = usable / long_loss_per_unit
    if raw_reduce_qty <= 0:
        return
    remaining_gap = _remaining_gap(long_qty, normal_short)
    if remaining_gap <= 0:
        return
    reduce_qty = min(raw_reduce_qty, remaining_gap)
    if reduce_qty <= 0:
        return
    # Net-short guard: make sure long stays >= normal short qty.
    if long_qty - reduce_qty < normal_short:
        reduce_qty = max(0.0, long_qty - normal_short)
        if reduce_qty <= 0:
            return
    # Snapshot addon and main state before reduce for audit.
    before = _snapshot_addon_state(state)
    long_qty_before = float(sim.book.long_qty or 0.0)
    normal_short_before = float(sim.book.short_qty or 0.0)
    recorder = _get_audit_recorder(sim)
    pre_fill_count = len(recorder.fills) if recorder is not None and recorder.enabled else 0
    # Use SimulatedOrderBook.apply_fill to execute a synthetic reduce-only fill.
    book = sim.book
    intent = StrategyIntent(
        side="long",
        qty=reduce_qty,
        purpose=ADDON_LONG_REDUCE_PURPOSE,
        order_type="Market",
        reduce_only=True,
        trigger_price=None,
        price=None,
    )
    order, _ = book.submit_intent(intent, replace=False)
    # Fill at current close price of candle.
    order_filled, _pnl = book.apply_fill(
        order_id=order.order_id,
        fill_price=float(close_price),
        qty=reduce_qty,
    )
    book.sync_runtime_state(sim.runtime_state)
    sim._record_order_event(
        order_filled,
        event_type="filled",
        status="FILLED",
    )
    fill_event = virtual_order_to_fill_event(
        order_filled,
        fill_price=float(close_price),
        occurred_at=candle.timestamp,
    )
    # Append to main backtest fill_log via result helper.
    from .historical_backtest import _append_fill_logs  # local import to avoid cycles

    pnl_delta = _append_fill_logs(
        result,
        sim,
        [fill_event],
        candle=candle,
        candle_index=candle_index,
    )
    tracker.state.long_reduce_total_qty += reduce_qty
    tracker.state.long_reduce_total_pnl += pnl_delta
    tracker.events.append(
        AddonShortRecoveryEvent(
            event_type=ADDON_LONG_REDUCE_PURPOSE,
            trade_index=state.addon_short_trade_count,
            close_timestamp=_now_iso(candle.timestamp),
            close_candle_index=candle_index,
            long_reduce_qty=reduce_qty,
            long_reduce_pnl=pnl_delta,
            long_reduce_price=float(close_price),
        )
    )
    after = _snapshot_addon_state(state)
    long_qty_after = float(sim.book.long_qty or 0.0)
    normal_short_after = float(sim.book.short_qty or 0.0)
    related_fill = None
    if recorder is not None and recorder.enabled:
        new_records = recorder.fills[pre_fill_count:]
        candidates = [r for r in new_records if r.order_id == order.order_id]
        if len(candidates) == 1:
            related_fill = candidates[0]

    rec = _record_addon_event(
        sim=sim,
        tracker=tracker,
        event_type="ADDON_LONG_REDUCE",
        event_reason="tp_long_reduce",
        candle=candle,
        candle_index=candle_index,
        before=before,
        after=after,
        extra={
            "configured_profit_usage_fraction": float(cfg.long_reduce_profit_usage_fraction),
            "short_profit_available": float(short_trade_pnl),
            "short_profit_usable": float(usable),
            "long_loss_per_unit": float(long_loss_per_unit),
            "raw_reduce_qty": float(raw_reduce_qty),
            "requested_reduce_qty": float(reduce_qty),
            "executed_reduce_qty": float(reduce_qty),
            "reduce_price": float(close_price),
            "long_avg_before_reduce": float(long_avg),
            "long_qty_before_reduce": float(long_qty_before),
            "long_qty_after_reduce": float(long_qty_after),
            "long_reduce_closed_pnl": float(pnl_delta),
            "associated_addon_trade_id": int(state.addon_short_trade_count),
            "associated_addon_close_event_sequence": int(
                state.last_addon_close_audit_event_sequence or 0
            )
            if state.last_addon_close_audit_event_sequence is not None
            else None,
            "related_fill_order_id": related_fill.order_id if related_fill is not None else None,
            "related_fill_event_sequence": (
                int(related_fill.global_event_sequence)
                if related_fill is not None
                else None
            ),
            "related_fill_event_sequence_in_candle": (
                int(related_fill.event_sequence_in_candle)
                if related_fill is not None
                else None
            ),
            # Precise main-book quantities and gaps before/after the reduce.
            "long_qty_before": float(long_qty_before),
            "normal_short_qty_before": float(normal_short_before),
            "long_qty_after": float(long_qty_after),
            "normal_short_qty_after": float(normal_short_after),
        },
    )
    # rec is currently unused by runtime; it is returned for audit-only purposes.


def process_addon_short_recovery_on_candle(
    *,
    sim: "HedgeBotOriginalSimulator",
    result: "BacktestResult",
    tracker: AddonShortRecoveryTracker | None,
    candle: SyntheticCandle,
    candle_index: int,
    candle_fills: Iterable[FillEvent],
) -> None:
    """Main per-candle entry point used by run_historical_backtest."""
    if tracker is None or not tracker.config.enabled:
        return
    cfg = tracker.config
    state = tracker.state

    # Update live position snapshot for analysis/export.
    _update_state_with_book(tracker, sim.book)

    # Activation check on this candle's fills.
    _maybe_activate_on_fills(
        sim=sim,
        tracker=tracker,
        fills=candle_fills,
        candle=candle,
        candle_index=candle_index,
    )

    if not state.activated:
        return

    # On activation candle: open first addon short immediately at activation price
    # when configured distance is 0.0.
    if (
        state.activation_candle_index == candle_index
        and not state.has_open_addon_short
        and float(cfg.addon_short_first_entry_distance_pct) == 0.0
    ):
        _open_addon_short_immediately_at_activation_price(sim=sim, tracker=tracker)

    # Remember trailing low from previous candles for rebound logic.
    previous_low = state.previous_low
    # First, try to close existing addon short on this candle.
    closed, close_price, close_reason = _maybe_close_addon_short_on_candle(
        sim=sim,
        tracker=tracker,
        candle=candle,
        candle_index=candle_index,
        previous_low_for_trailing=previous_low,
    )

    # If TP close happened, apply long reduction.
    if closed and close_reason == "tp" and close_price is not None:
        # The last appended TP event holds the short trade PnL.
        last_ev = tracker.events[-1]
        short_pnl = float(last_ev.net_pnl or 0.0)
        _maybe_long_reduce_after_tp(
            sim=sim,
            result=result,
            tracker=tracker,
            close_price=close_price,
            short_trade_pnl=short_pnl,
            candle=candle,
            candle_index=candle_index,
        )

    # After close we enforce cooldown: no same-candle re-entry.
    if state.cooldown_after_close:
        # Cooldown applies only for this candle.
        state.cooldown_after_close = False
        # Update trailing low with current candle AFTER close; used from next candle.
        _update_trailing_low_for_candle(
            tracker=tracker,
            candle=candle,
            use_previous_only=False,
        )
        state.previous_low = state.lowest_price_since_entry or state.previous_low
        return

    # Maintain trailing low including this candle for future rebound decisions.
    _update_trailing_low_for_candle(
        tracker=tracker,
        candle=candle,
        use_previous_only=False,
    )
    state.previous_low = state.lowest_price_since_entry or state.previous_low

    # Decide on new entry if no addon short is open.
    if not state.has_open_addon_short:
        long_qty = float(sim.book.long_qty or 0.0)
        normal_short = float(sim.book.short_qty or 0.0)
        remaining_gap = _remaining_gap(long_qty, normal_short)
        if remaining_gap <= 0:
            # Maybe mark completion if conditions are satisfied.
            if (
                cfg.stop_when_long_qty_reaches_normal_short_qty
                and long_qty <= normal_short
                and not state.has_open_addon_short
            ):
                before = _snapshot_addon_state(state)
                state.recovery_completed = True
                state.recovery_completion_reason = "long_qty<=short_qty_no_addon_short_open"
                state.recovery_completed_candle_index = candle_index
                after = _snapshot_addon_state(state)
                _record_addon_event(
                    sim=sim,
                    tracker=tracker,
                    event_type="RECOVERY_COMPLETED",
                    event_reason=state.recovery_completion_reason,
                    candle=candle,
                    candle_index=candle_index,
                    before=before,
                    after=after,
                    extra=None,
                )
            return

        step_qty = float(state.addon_short_step_qty or 0.0)
        if step_qty <= 0:
            return
        desired_qty = min(step_qty, remaining_gap)
        if desired_qty <= 0:
            return
        # Distinguish between first distance-based entry and true reentries.
        is_reentry = state.addon_short_trade_count > 0
        if not cfg.allow_net_short and long_qty < normal_short + desired_qty:
            desired_qty = max(0.0, long_qty - normal_short)
            if desired_qty <= 0:
                return

        # Distance-based first/next entry: use either activation price or
        # previous reference low/entry as configured.
        ref_price = state.activation_price or float(candle.close)
        if cfg.addon_short_reentry_reference == "previous_low" and state.previous_low:
            ref_price = state.previous_low
        elif cfg.addon_short_reentry_reference == "previous_tp" and state.previous_tp_price:
            ref_price = state.previous_tp_price
        elif cfg.addon_short_reentry_reference == "previous_entry" and state.previous_entry_price:
            ref_price = state.previous_entry_price

        entry_distance_pct = float(cfg.addon_short_first_entry_distance_pct)
        if is_reentry:
            # For re-entries we always use reentry_buffer_pct relative to ref price.
            entry_distance_pct = float(cfg.addon_short_reentry_buffer_pct)

        target_entry_price = ref_price * (1.0 - entry_distance_pct / 100.0)
        low = float(candle.low if candle.low is not None else candle.close)
        if low > target_entry_price:
            # Trigger not reached on this candle.
            return

        before = _snapshot_addon_state(state)
        # Open new addon short at target_entry_price.
        state.has_open_addon_short = True
        state.addon_short_qty_open = desired_qty
        state.addon_short_entry_price = float(target_entry_price)
        state.addon_short_entry_timestamp = _now_iso(candle.timestamp)
        state.addon_short_entry_candle_index = candle_index
        state.lowest_price_since_entry = float(target_entry_price)
        state.maximum_favorable_move_pct = 0.0
        state.previous_entry_price = target_entry_price
        state.addon_short_trade_count += 1
        tracker.events.append(
            AddonShortRecoveryEvent(
                event_type=ADDON_SHORT_ENTRY_PURPOSE,
                trade_index=state.addon_short_trade_count,
                entry_timestamp=state.addon_short_entry_timestamp,
                entry_candle_index=state.addon_short_entry_candle_index,
                entry_price=state.addon_short_entry_price,
                entry_qty=desired_qty,
                activation_price=state.activation_price,
                activation_timestamp=state.activation_timestamp,
            )
        )
        after = _snapshot_addon_state(state)
        event_type = "ADDON_SHORT_REENTRY" if is_reentry else "ADDON_SHORT_FIRST_ENTRY"
        _record_addon_event(
            sim=sim,
            tracker=tracker,
            event_type=event_type,
            event_reason="distance_entry_reentry" if is_reentry else "distance_entry_first",
            candle=candle,
            candle_index=candle_index,
            before=before,
            after=after,
            extra={
                "first_entry_or_reentry": "reentry" if is_reentry else "first_entry",
                "requested_entry_qty": float(desired_qty),
                "executed_entry_qty": float(desired_qty),
                "entry_price": state.addon_short_entry_price,
                "entry_trigger_price": float(target_entry_price),
                "entry_reference_low": float(ref_price),
                "entry_distance_pct": float(entry_distance_pct),
                "reentry_buffer_pct": float(cfg.addon_short_reentry_buffer_pct)
                if is_reentry
                else None,
                "remaining_gap_before_entry": float(remaining_gap),
                "remaining_gap_after_entry": _remaining_gap(
                    long_qty,
                    normal_short + float(state.addon_short_qty_open or 0.0),
                ),
            },
        )

    # Completion check when feature is configured to stop once long<=short and
    # no addon short is open.
    long_qty = float(sim.book.long_qty or 0.0)
    normal_short = float(sim.book.short_qty or 0.0)
    if (
        cfg.stop_when_long_qty_reaches_normal_short_qty
        and long_qty <= normal_short
        and not state.has_open_addon_short
        and not state.recovery_completed
    ):
        before = _snapshot_addon_state(state)
        state.recovery_completed = True
        state.recovery_completion_reason = "long_qty<=short_qty_no_addon_short_open"
        state.recovery_completed_candle_index = candle_index
        after = _snapshot_addon_state(state)
        _record_addon_event(
            sim=sim,
            tracker=tracker,
            event_type="RECOVERY_COMPLETED",
            event_reason=state.recovery_completion_reason,
            candle=candle,
            candle_index=candle_index,
            before=before,
            after=after,
            extra=None,
        )


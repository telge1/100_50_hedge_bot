"""Backtest-only simulator hook for stuck SHORT_REDUCE recovery reload."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fixed_cycle_hedge_bot.models import FillEvent, StrategyIntent

from .stuck_recovery_reload import (
    StuckRecoveryReloadConfig,
    StuckRecoveryReloadRecord,
    StuckRecoveryReloadTrigger,
    build_stuck_recovery_reload_metadata,
    resolve_reload_notionals,
    should_trigger_stuck_recovery_reload,
)
from .simulated_execution import fill_order_at_candle_close, is_stuck_recovery_reload_market_fill

if TYPE_CHECKING:
    from .hedge_bot_original_simulator import HedgeBotOriginalSimulator

STUCK_RELOAD_LONG_PURPOSE = "STUCK_RECOVERY_RELOAD_LONG_ENTRY"
STUCK_RELOAD_SHORT_PURPOSE = "STUCK_RECOVERY_RELOAD_SHORT_ENTRY"


@dataclass
class StuckRecoveryReloadTracker:
    config: StuckRecoveryReloadConfig = field(default_factory=StuckRecoveryReloadConfig)
    record: StuckRecoveryReloadRecord = field(default_factory=StuckRecoveryReloadRecord)
    last_fill_candle_index: int | None = None

    def note_fills(self, *, candle_index: int, fill_count: int) -> None:
        if fill_count > 0:
            self.last_fill_candle_index = candle_index

    def candles_since_last_fill(self, candle_index: int) -> int:
        if self.last_fill_candle_index is None:
            return 0
        return max(0, candle_index - self.last_fill_candle_index)


def _is_exit_or_cycle_purpose(purpose: str) -> bool:
    normalized = str(purpose or "").strip().upper()
    if normalized in {"LONG_TP_EXIT", "SHORT_SL_EXIT"}:
        return True
    return normalized.startswith("CYCLE_")


def _cancel_exit_and_cycle_orders(sim: HedgeBotOriginalSimulator) -> list[str]:
    canceled: list[str] = []
    for order in list(sim.book.active_orders()):
        if not _is_exit_or_cycle_purpose(str(order.purpose or "")):
            continue
        if not sim.book.cancel_by_order_id(order.order_id):
            continue
        canceled.append(str(order.purpose or ""))
        sim._record_order_event(
            order,
            event_type="cancelled",
            status="CANCELED",
        )
    if canceled:
        sim.book.sync_runtime_state(sim.runtime_state)
        sim._refresh_snapshot_from_book(source="after_stuck_reload_cancel", price=sim.candle.close)
    return canceled


def _reconcile_strategy_state_after_reload(
    strategy: Any,
    runtime_state: Any,
    *,
    cycle_index: int,
) -> None:
    state = runtime_state.strategy_state
    cycle_state = strategy._ensure_cycle_state(runtime_state)
    state["force_exit_rebuild"] = True
    state["exit_rebuild_allowed"] = True
    if hasattr(strategy, "_set_second_leg_waiting_state"):
        strategy._set_second_leg_waiting_state(
            state,
            cycle_state,
            waiting=False,
            cycle_index=cycle_index,
        )
    state.pop("short_tp_pending_cycle", None)
    cycle_state.pop("short_tp_pending_cycle", None)
    state.pop("pending_short_cycle_index", None)
    cycle_state.pop("pending_short_cycle_index", None)
    if hasattr(strategy, "_write_cycle_state"):
        strategy._write_cycle_state(cycle_state)


def execute_stuck_recovery_reload(
    sim: HedgeBotOriginalSimulator,
    *,
    config: StuckRecoveryReloadConfig,
    trigger: StuckRecoveryReloadTrigger,
    reload_count_for_trade: int,
) -> tuple[list[FillEvent], StuckRecoveryReloadRecord]:
    strategy = sim.strategy
    runtime_state = sim.runtime_state
    state = runtime_state.strategy_state
    fill_price = float(sim.candle.close)

    long_notional, short_notional = resolve_reload_notionals(config, state, sim.config)
    long_price = float(sim.book.long_avg or fill_price)
    short_price = float(sim.book.short_avg or fill_price)
    if long_price <= 0:
        long_price = fill_price
    if short_price <= 0:
        short_price = fill_price

    long_qty = float(
        strategy._price_to_qty(
            notional_usdt=long_notional,
            price=long_price,
            runtime_state=runtime_state,
        )
    )
    short_qty = float(
        strategy._price_to_qty(
            notional_usdt=short_notional,
            price=short_price,
            runtime_state=runtime_state,
        )
    )

    active_before = [
        str(order.purpose or "")
        for order in sim.book.active_orders()
        if str(order.purpose or "")
    ]
    _cancel_exit_and_cycle_orders(sim)

    metadata_base = build_stuck_recovery_reload_metadata(
        config=config,
        record=StuckRecoveryReloadRecord(
            reload_count_for_trade=reload_count_for_trade,
            reload_cycle_index=trigger.cycle_index,
            reload_reason="stuck_cycle_short_reduce",
            reload_candles_since_last_fill=trigger.candles_since_last_fill,
            reload_realized_pnl_before=trigger.realized_pnl_before,
            reload_long_notional_usdt=long_notional,
            reload_short_notional_usdt=short_notional,
            active_purpose_before_reload=trigger.active_purpose,
            stuck_recovery_reload_triggered=True,
        ),
        trigger=trigger,
    )

    fills: list[FillEvent] = []
    deferred_on_fill_followups: list[StrategyIntent] = []
    for side, purpose, qty in (
        ("long", STUCK_RELOAD_LONG_PURPOSE, long_qty),
        ("short", STUCK_RELOAD_SHORT_PURPOSE, short_qty),
    ):
        if qty <= 0:
            continue
        intent = StrategyIntent(
            side=side,
            qty=qty,
            purpose=purpose,
            order_type="Market",
            reduce_only=False,
            position_idx=1 if side == "long" else 2,
            metadata={
                **metadata_base,
                "source": "stuck_recovery_reload",
                "reload_cycle_index": trigger.cycle_index,
            },
        )
        intent_index = sim._log_intent(intent, event_source="stuck_recovery_reload")
        order = sim._submit_intent_with_logging(
            intent,
            replace=True,
            intent_log_index=intent_index,
        )
        if order is None:
            continue
        if is_stuck_recovery_reload_market_fill(intent):
            fill_event = fill_order_at_candle_close(
                book=sim.book,
                runtime_state=runtime_state,
                order_id=order.order_id,
                candle=sim.candle,
            )
            fill_event.metadata = dict(fill_event.metadata or {})
            fill_event.metadata.update(metadata_base)
            filled_order = sim.book.get_order(fill_event.client_order_id)
            if filled_order is not None:
                sim._record_order_event(
                    filled_order,
                    event_type="filled",
                    status="FILLED",
                )
            sim.book.sync_runtime_state(runtime_state)
            sim._refresh_snapshot_from_book(
                source="after_stuck_recovery_reload_fill",
                price=fill_price,
            )
            # Keep strategy state in sync, but defer any per-fill follow-up intents
            # until both reload legs are filled. Submitting exits after only the
            # long reload would build SHORT_SL_EXIT against stale short qty.
            follow_up = strategy.on_fill(
                fill_event,
                sim.snapshot,
                runtime_state,
                sim.context,
            ) or []
            if follow_up:
                deferred_on_fill_followups.extend(follow_up)
            fills.append(fill_event)

    sim._refresh_snapshot_from_book(source="after_stuck_recovery_reload_fills", price=fill_price)
    _reconcile_strategy_state_after_reload(
        strategy,
        runtime_state,
        cycle_index=trigger.cycle_index,
    )
    sim._refresh_snapshot_from_book(source="after_stuck_recovery_reload_reconcile", price=fill_price)

    tick_intents = strategy.on_tick(sim.snapshot, runtime_state, sim.context) or []
    if tick_intents:
        sim.submit_intents_to_book(tick_intents, event_source="after_stuck_recovery_reload_tick")
        sim._refresh_snapshot_from_book(source="after_stuck_recovery_reload_rebuild", price=fill_price)

    active_after = [
        str(order.purpose or "")
        for order in sim.book.active_orders()
        if str(order.purpose or "")
    ]

    record = StuckRecoveryReloadRecord(
        reload_count_for_trade=reload_count_for_trade,
        reload_cycle_index=trigger.cycle_index,
        reload_reason="stuck_cycle_short_reduce",
        reload_candles_since_last_fill=trigger.candles_since_last_fill,
        reload_realized_pnl_before=trigger.realized_pnl_before,
        reload_long_notional_usdt=long_notional,
        reload_short_notional_usdt=short_notional,
        reload_long_qty=long_qty,
        reload_short_qty=short_qty,
        active_purpose_before_reload=trigger.active_purpose,
        active_purposes_after_reload=active_after,
        stuck_recovery_reload_triggered=True,
    )
    return fills, record


def maybe_execute_stuck_recovery_reload(
    sim: HedgeBotOriginalSimulator,
    tracker: StuckRecoveryReloadTracker,
    *,
    cumulative_pnl: float,
    candle_index: int,
    trade_closed: bool,
) -> list[FillEvent]:
    config = tracker.config
    if not config.enabled:
        return []
    should_reload, trigger = should_trigger_stuck_recovery_reload(
        sim,
        config=config,
        cumulative_pnl=cumulative_pnl,
        candles_since_last_fill=tracker.candles_since_last_fill(candle_index),
        reload_count_for_trade=tracker.record.reload_count_for_trade,
        trade_closed=trade_closed,
    )
    if not should_reload or trigger is None:
        return []

    next_count = tracker.record.reload_count_for_trade + 1
    fills, record = execute_stuck_recovery_reload(
        sim,
        config=config,
        trigger=trigger,
        reload_count_for_trade=next_count,
    )
    if not record.stuck_recovery_reload_triggered:
        return []
    tracker.record = record
    tracker.last_fill_candle_index = candle_index
    return fills


def attach_stuck_recovery_reload_tracker(
    sim: HedgeBotOriginalSimulator,
    config: StuckRecoveryReloadConfig | None,
) -> StuckRecoveryReloadTracker | None:
    if config is None or not config.enabled:
        sim.stuck_recovery_reload_tracker = None
        return None
    tracker = StuckRecoveryReloadTracker(config=config)
    sim.stuck_recovery_reload_tracker = tracker
    return tracker

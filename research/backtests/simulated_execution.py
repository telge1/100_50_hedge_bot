"""Virtual fill execution for backtest harness (Phase 2)."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fixed_cycle_hedge_bot.models import FillEvent, RuntimeState, StrategyIntent

from .simulated_order_book import (
    ACTIVE_ORDER_STATUSES,
    SimulatedOrderBook,
    SyntheticCandle,
    VirtualOrder,
)

INITIAL_ENTRY_PURPOSES = frozenset({"INITIAL_LONG_ENTRY", "INITIAL_SHORT_ENTRY"})


def is_immediate_market_fill(intent: StrategyIntent) -> bool:
    """Only initial hedge entries are filled immediately in Phase 2."""
    purpose = str(intent.purpose or "").strip().upper()
    return purpose in INITIAL_ENTRY_PURPOSES


def submit_intent_to_book(
    book: SimulatedOrderBook,
    runtime_state: RuntimeState,
    intent: StrategyIntent,
    *,
    replace: bool = True,
) -> VirtualOrder:
    order = book.submit_intent(intent, replace=replace)
    book.sync_runtime_state(runtime_state)
    return order


def submit_resting_intents(
    book: SimulatedOrderBook,
    runtime_state: RuntimeState,
    intents: list[StrategyIntent],
    *,
    replace: bool = True,
) -> list[VirtualOrder]:
    submitted: list[VirtualOrder] = []
    for intent in intents:
        if is_immediate_market_fill(intent):
            continue
        submitted.append(submit_intent_to_book(book, runtime_state, intent, replace=replace))
    return submitted


def virtual_order_to_fill_event(order: VirtualOrder, *, fill_price: float) -> FillEvent:
    metadata = dict(order.metadata or {})
    metadata.setdefault("source", "simulated_execution")
    metadata.setdefault("fill_price", fill_price)
    metadata.setdefault("confirmed_closed_pnl", 0.0)
    metadata.setdefault("closed_pnl", 0.0)
    metadata.setdefault("runtime_calculated_pnl", 0.0)
    metadata.setdefault("exec_pnl", 0.0)
    metadata.setdefault("symbol", order.symbol)

    return FillEvent(
        exchange_order_id=order.exchange_order_id,
        client_order_id=order.order_id,
        side=order.side,
        purpose=order.purpose,
        exec_qty=float(order.qty),
        exec_price=float(fill_price),
        order_type=order.order_type,
        reduce_only=bool(order.reduce_only),
        status="FILLED",
        exec_id=f"sim-exec-{uuid4().hex[:12]}",
        metadata=metadata,
        occurred_at=datetime.now(timezone.utc),
    )


def fill_order_at_candle_close(
    *,
    book: SimulatedOrderBook,
    runtime_state: RuntimeState,
    order_id: str,
    candle: SyntheticCandle,
) -> FillEvent:
    fill_price = float(candle.close)
    order = book.apply_market_fill(order_id=order_id, fill_price=fill_price)
    runtime_state.active_orders.pop(order_id, None)
    if order.exchange_order_id:
        runtime_state.terminal_exchange_ids.add(order.exchange_order_id)
    runtime_state.terminal_client_ids.add(order_id)
    book.sync_runtime_state(runtime_state)
    return virtual_order_to_fill_event(order, fill_price=fill_price)


def fill_entry_intents_at_candle_close(
    *,
    book: SimulatedOrderBook,
    runtime_state: RuntimeState,
    intents: list[StrategyIntent],
    candle: SyntheticCandle,
) -> list[tuple[str, FillEvent]]:
    filled: list[tuple[str, FillEvent]] = []
    for intent in intents:
        if not is_immediate_market_fill(intent):
            submit_intent_to_book(book, runtime_state, intent)
            continue
        order = submit_intent_to_book(book, runtime_state, intent, replace=False)
        fill_event = fill_order_at_candle_close(
            book=book,
            runtime_state=runtime_state,
            order_id=order.order_id,
            candle=candle,
        )
        filled.append((order.order_id, fill_event))
    return filled


# Backward-compatible aliases for Phase-1 callers.
fill_intent_at_candle_close = fill_order_at_candle_close
fill_intents_at_candle_close = fill_entry_intents_at_candle_close

"""Virtual fill execution for backtest harness (Phase 3)."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fixed_cycle_hedge_bot.models import FillEvent, RuntimeState, StrategyIntent

from .simulated_order_book import SimulatedOrderBook, SyntheticCandle, VirtualOrder
from .simulated_pnl import attach_closed_pnl_metadata

INITIAL_ENTRY_PURPOSES = frozenset({"INITIAL_LONG_ENTRY", "INITIAL_SHORT_ENTRY"})


def is_immediate_market_fill(intent: StrategyIntent) -> bool:
    """Only initial hedge entries are filled immediately at candle close."""
    purpose = str(intent.purpose or "").strip().upper()
    return purpose in INITIAL_ENTRY_PURPOSES


def order_trigger_side(order: VirtualOrder) -> str:
    """Map virtual order to buy/sell trigger semantics for high/low checks."""
    side = str(order.side).lower()
    if side == "long":
        return "sell" if order.reduce_only else "buy"
    if side == "short":
        return "buy" if order.reduce_only else "sell"
    raise ValueError(f"unsupported order side for trigger check: {order.side}")


def resolve_order_check_and_fill_prices(order: VirtualOrder) -> tuple[float, float]:
    trigger = order.trigger_price
    price = order.price
    if trigger is not None and price is not None:
        return float(trigger), float(price)
    if trigger is not None:
        value = float(trigger)
        return value, value
    if price is not None:
        value = float(price)
        return value, value
    raise ValueError(f"order {order.order_id} has no price or trigger_price")


def should_fill_order_on_candle(order: VirtualOrder, candle: SyntheticCandle) -> bool:
    check_price, _ = resolve_order_check_and_fill_prices(order)
    direction = order_trigger_side(order)
    low = float(candle.low if candle.low is not None else candle.close)
    high = float(candle.high if candle.high is not None else candle.close)
    if direction == "buy":
        return low <= check_price
    return high >= check_price


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


def virtual_order_to_fill_event(
    order: VirtualOrder,
    *,
    fill_price: float,
    occurred_at: datetime | None = None,
) -> FillEvent:
    metadata = dict(order.metadata or {})
    metadata.setdefault("source", "simulated_execution")
    metadata.setdefault("fill_price", fill_price)
    metadata.setdefault("symbol", order.symbol)
    metadata.setdefault("order_id", order.order_id)
    metadata.setdefault("exchange_order_id", order.exchange_order_id)
    metadata.setdefault("purpose", order.purpose)
    if metadata.get("cycle_index") is None and order.metadata.get("cycle_index") is not None:
        metadata["cycle_index"] = order.metadata.get("cycle_index")
    if metadata.get("cycle_role") is None and order.metadata.get("cycle_role") is not None:
        metadata["cycle_role"] = order.metadata.get("cycle_role")

    pnl = float(metadata.get("confirmed_closed_pnl") or metadata.get("closed_pnl") or 0.0)
    attach_closed_pnl_metadata(metadata, pnl)

    return FillEvent(
        exchange_order_id=order.exchange_order_id,
        client_order_id=order.order_id,
        side=order.side,
        purpose=order.purpose,
        exec_qty=float(order.filled_qty or order.qty),
        exec_price=float(fill_price),
        order_type=order.order_type,
        reduce_only=bool(order.reduce_only),
        status="FILLED",
        exec_id=f"sim-exec-{uuid4().hex[:12]}",
        metadata=metadata,
        occurred_at=occurred_at or datetime.now(timezone.utc),
    )


def _mark_order_terminal(runtime_state: RuntimeState, order: VirtualOrder) -> None:
    runtime_state.active_orders.pop(order.order_id, None)
    runtime_state.terminal_client_ids.add(order.order_id)
    if order.exchange_order_id:
        runtime_state.terminal_exchange_ids.add(order.exchange_order_id)


def fill_order_at_price(
    *,
    book: SimulatedOrderBook,
    runtime_state: RuntimeState,
    order_id: str,
    fill_price: float,
    occurred_at: datetime | None = None,
) -> FillEvent:
    order, _ = book.apply_fill(order_id=order_id, fill_price=fill_price)
    _mark_order_terminal(runtime_state, order)
    book.sync_runtime_state(runtime_state)
    return virtual_order_to_fill_event(order, fill_price=fill_price, occurred_at=occurred_at)


def fill_order_at_candle_close(
    *,
    book: SimulatedOrderBook,
    runtime_state: RuntimeState,
    order_id: str,
    candle: SyntheticCandle,
) -> FillEvent:
    return fill_order_at_price(
        book=book,
        runtime_state=runtime_state,
        order_id=order_id,
        fill_price=float(candle.close),
        occurred_at=candle.timestamp,
    )


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


def process_candle_fills(
    *,
    book: SimulatedOrderBook,
    runtime_state: RuntimeState,
    candle: SyntheticCandle,
) -> list[FillEvent]:
    """Check active resting orders against candle high/low and emit FillEvents."""
    fill_events: list[FillEvent] = []
    for order in list(book.active_orders()):
        if not should_fill_order_on_candle(order, candle):
            continue
        _, fill_price = resolve_order_check_and_fill_prices(order)
        fill_events.append(
            fill_order_at_price(
                book=book,
                runtime_state=runtime_state,
                order_id=order.order_id,
                fill_price=fill_price,
                occurred_at=candle.timestamp,
            )
        )
    return fill_events


# Backward-compatible aliases for Phase-1 callers.
fill_intent_at_candle_close = fill_order_at_candle_close
fill_intents_at_candle_close = fill_entry_intents_at_candle_close

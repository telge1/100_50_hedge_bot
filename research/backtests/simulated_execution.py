"""Virtual fill execution for Phase-1 backtest smoke tests."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fixed_cycle_hedge_bot.models import FillEvent, ManagedOrder, RuntimeState, StrategyIntent

from .simulated_order_book import SimulatedOrderBook, SyntheticCandle


def register_intent_in_book(
    book: SimulatedOrderBook,
    intent: StrategyIntent,
    *,
    client_order_id: str | None = None,
) -> str:
    cid = client_order_id or book.next_client_order_id(intent.purpose)
    book.register_intent(
        client_order_id=cid,
        side=intent.side,
        qty=float(intent.qty),
        purpose=intent.purpose,
        price=intent.price,
        order_type=intent.order_type,
        reduce_only=bool(intent.reduce_only),
        metadata=dict(intent.metadata or {}),
    )
    return cid


def register_intent_in_runtime(
    runtime_state: RuntimeState,
    intent: StrategyIntent,
    *,
    client_order_id: str,
    exchange_order_id: str | None = None,
) -> ManagedOrder:
    exchange_id = exchange_order_id or f"sim-ex-{uuid4().hex[:12]}"
    managed = ManagedOrder(
        client_order_id=client_order_id,
        exchange_order_id=exchange_id,
        side=intent.side,
        qty=float(intent.qty),
        purpose=intent.purpose,
        price=intent.price,
        order_type=intent.order_type,
        reduce_only=bool(intent.reduce_only),
        status="NEW",
        metadata=dict(intent.metadata or {}),
    )
    runtime_state.active_orders[client_order_id] = managed
    runtime_state.exchange_to_client_id[exchange_id] = client_order_id
    runtime_state.client_to_exchange_id[client_order_id] = exchange_id
    return managed


def fill_intent_at_candle_close(
    *,
    book: SimulatedOrderBook,
    runtime_state: RuntimeState,
    client_order_id: str,
    candle: SyntheticCandle,
) -> FillEvent:
    fill_price = float(candle.close)
    fill_payload = book.apply_market_fill(client_order_id=client_order_id, fill_price=fill_price)
    managed = runtime_state.active_orders.get(client_order_id)
    exchange_order_id = (
        str(managed.exchange_order_id)
        if managed and managed.exchange_order_id
        else f"sim-ex-{client_order_id}"
    )
    if managed is not None:
        managed.status = "FILLED"
        managed.filled_qty = float(fill_payload["exec_qty"])
        managed.remaining_qty = 0.0
        runtime_state.active_orders.pop(client_order_id, None)
        runtime_state.terminal_client_ids.add(client_order_id)
        if exchange_order_id:
            runtime_state.terminal_exchange_ids.add(exchange_order_id)

    metadata = dict(fill_payload.get("metadata") or {})
    metadata.setdefault("source", "simulated_execution")
    metadata.setdefault("fill_price", fill_price)
    metadata.setdefault("confirmed_closed_pnl", 0.0)
    metadata.setdefault("closed_pnl", 0.0)
    metadata.setdefault("runtime_calculated_pnl", 0.0)
    metadata.setdefault("exec_pnl", 0.0)
    metadata.setdefault("symbol", candle.symbol)

    return FillEvent(
        exchange_order_id=exchange_order_id,
        client_order_id=client_order_id,
        side=str(fill_payload["side"]),
        purpose=str(fill_payload["purpose"]),
        exec_qty=float(fill_payload["exec_qty"]),
        exec_price=float(fill_payload["exec_price"]),
        order_type=str(fill_payload.get("order_type") or "Market"),
        reduce_only=bool(fill_payload.get("reduce_only")),
        status="FILLED",
        exec_id=f"sim-exec-{uuid4().hex[:12]}",
        metadata=metadata,
        occurred_at=datetime.now(timezone.utc),
    )


def fill_intents_at_candle_close(
    *,
    book: SimulatedOrderBook,
    runtime_state: RuntimeState,
    intents: list[StrategyIntent],
    candle: SyntheticCandle,
) -> list[tuple[str, FillEvent]]:
    filled: list[tuple[str, FillEvent]] = []
    for intent in intents:
        client_order_id = register_intent_in_book(book, intent)
        register_intent_in_runtime(runtime_state, intent, client_order_id=client_order_id)
        fill_event = fill_intent_at_candle_close(
            book=book,
            runtime_state=runtime_state,
            client_order_id=client_order_id,
            candle=candle,
        )
        filled.append((client_order_id, fill_event))
    return filled

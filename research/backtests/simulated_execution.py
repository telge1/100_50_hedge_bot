"""Virtual fill execution for backtest harness (Phase 3)."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fixed_cycle_hedge_bot.models import FillEvent, RuntimeState, StrategyIntent

from .purpose_utils import enrich_purpose_metadata, preserve_bot_purpose
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
    order, _ = book.submit_intent(intent, replace=replace)
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
    metadata = enrich_purpose_metadata(order.purpose, dict(order.metadata or {}))
    metadata.setdefault("source", "simulated_execution")
    metadata.setdefault("fill_price", fill_price)
    metadata.setdefault("symbol", order.symbol)
    metadata.setdefault("order_id", order.order_id)
    metadata.setdefault("exchange_order_id", order.exchange_order_id)

    pnl = float(metadata.get("confirmed_closed_pnl") or metadata.get("closed_pnl") or 0.0)
    attach_closed_pnl_metadata(metadata, pnl)

    return FillEvent(
        exchange_order_id=order.exchange_order_id,
        client_order_id=order.order_id,
        side=order.side,
        purpose=preserve_bot_purpose(order.purpose),
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
    order_before = book.get_order(order_id)
    order_check_price: float | None = None
    if order_before is not None:
        try:
            order_check_price, _ = resolve_order_check_and_fill_prices(order_before)
        except ValueError:
            order_check_price = float(fill_price)
    else:
        order_check_price = float(fill_price)
    order, _ = book.apply_fill(order_id=order_id, fill_price=fill_price)
    _mark_order_terminal(runtime_state, order)
    book.sync_runtime_state(runtime_state)
    fill_event = virtual_order_to_fill_event(order, fill_price=fill_price, occurred_at=occurred_at)
    if order_check_price is not None:
        fill_event.metadata["order_check_price"] = float(order_check_price)
    return fill_event


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


def rank_conservative_fill_orders(
    orders: list[VirtualOrder],
    candle: SyntheticCandle,
) -> list[VirtualOrder]:
    """Rank touchable orders for a conservative 5m OHLC fill (Phase 4).

    With only 5m bars we do not know intracandle path. When multiple resting
    orders would fill on the same candle, we assume an adverse move first:

    - Buy triggers (``low <= check_price``) before sell triggers.
    - Among buy triggers: highest check price first (worst buy / pay more).
    - Among sell triggers: lowest check price first (worst sell / receive less).

    Tie-break: ``created_index``, then ``order_id``.
    """
    touchable = [order for order in orders if should_fill_order_on_candle(order, candle)]

    def _sort_key(order: VirtualOrder) -> tuple[int, float, int, str]:
        direction = order_trigger_side(order)
        check_price, _ = resolve_order_check_and_fill_prices(order)
        if direction == "buy":
            return (0, -float(check_price), int(order.created_index), order.order_id)
        return (1, float(check_price), int(order.created_index), order.order_id)

    return sorted(touchable, key=_sort_key)


def select_orders_to_fill_on_candle(
    orders: list[VirtualOrder],
    candle: SyntheticCandle,
    *,
    max_fills_per_candle: int | None = None,
    conservative_fill_order: bool = True,
) -> list[VirtualOrder]:
    """Return the subset of active orders that may fill on this candle."""
    if max_fills_per_candle is None:
        return [order for order in orders if should_fill_order_on_candle(order, candle)]

    if max_fills_per_candle <= 0:
        return []

    if conservative_fill_order:
        ranked = rank_conservative_fill_orders(orders, candle)
    else:
        ranked = [
            order
            for order in sorted(orders, key=lambda item: (item.created_index, item.order_id))
            if should_fill_order_on_candle(order, candle)
        ]
    return ranked[: int(max_fills_per_candle)]


def process_candle_fills(
    *,
    book: SimulatedOrderBook,
    runtime_state: RuntimeState,
    candle: SyntheticCandle,
    max_fills_per_candle: int | None = None,
    conservative_fill_order: bool = True,
) -> list[FillEvent]:
    """Check active resting orders against candle high/low and emit FillEvents.

    Phase 4 default for historical backtests: ``max_fills_per_candle=1`` so
    new orders from ``on_fill`` are not eligible until the next candle.
    Pass ``max_fills_per_candle=None`` for legacy unlimited fills (smoke tests).
    """
    candidates = select_orders_to_fill_on_candle(
        list(book.active_orders()),
        candle,
        max_fills_per_candle=max_fills_per_candle,
        conservative_fill_order=conservative_fill_order,
    )
    fill_events: list[FillEvent] = []
    for order in candidates:
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

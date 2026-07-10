"""Virtual fill execution for backtest harness (Phase 3)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from typing import Any

from fixed_cycle_hedge_bot.models import FillEvent, RuntimeState, StrategyIntent

from .fill_models import is_exit_purpose, normalize_fill_model
from .purpose_utils import enrich_purpose_metadata, preserve_bot_purpose
from .simulated_order_book import SimulatedOrderBook, SyntheticCandle, VirtualOrder
from .simulated_pnl import attach_closed_pnl_metadata, closed_pnl_for_virtual_order_fill

DEFAULT_SIMULATED_FEE_RATE = 0.00055


def resolve_simulated_fee_rate(config: Any | None = None) -> float:
    """Return decimal fee rate aligned with strategy ``order_fee_rate_pct`` (0.055 => 0.00055)."""
    if config is not None:
        pct = getattr(config, "order_fee_rate_pct", None)
        if pct is not None:
            return float(pct) / 100.0
    return DEFAULT_SIMULATED_FEE_RATE

INITIAL_ENTRY_PURPOSES = frozenset({"INITIAL_LONG_ENTRY", "INITIAL_SHORT_ENTRY"})
REFILL_MARKET_FILL_PURPOSES = frozenset({"REFILL_LONG", "REFILL_SHORT"})
STUCK_RECOVERY_RELOAD_MARKET_FILL_PURPOSES = frozenset(
    {"STUCK_RECOVERY_RELOAD_LONG_ENTRY", "STUCK_RECOVERY_RELOAD_SHORT_ENTRY"}
)


def _is_cycle_second_leg_reduce_purpose(purpose: str) -> bool:
    """Return True for CYCLE_N_SHORT_REDUCE / CYCLE_N_LONG_REDUCE purposes."""
    normalized = preserve_bot_purpose(purpose).strip().upper()
    if not normalized.startswith("CYCLE_"):
        return False
    return normalized.endswith("_SHORT_REDUCE") or normalized.endswith("_LONG_REDUCE")


@dataclass(frozen=True)
class OrderTouchResult:
    touched: bool
    trigger_touch_rule: str | None = None
    trigger_warning: str | None = None
    order_check_price: float | None = None
    fill_price: float | None = None


def is_immediate_refill_market_fill(intent: StrategyIntent) -> bool:
    """Cycle refill market orders (no price/trigger) fill immediately at candle close."""
    purpose = str(intent.purpose or "").strip().upper()
    if purpose not in REFILL_MARKET_FILL_PURPOSES:
        return False
    order_type = str(intent.order_type or "Market").strip().upper()
    return order_type == "MARKET" and intent.price is None and intent.trigger_price is None


def is_stuck_recovery_reload_market_fill(intent: StrategyIntent) -> bool:
    """Backtest-only stuck recovery reload entries fill at candle close."""
    purpose = str(intent.purpose or "").strip().upper()
    if purpose not in STUCK_RECOVERY_RELOAD_MARKET_FILL_PURPOSES:
        return False
    order_type = str(intent.order_type or "Market").strip().upper()
    return order_type == "MARKET" and intent.price is None and intent.trigger_price is None


def is_immediate_cycle_second_leg_market_fill(intent: StrategyIntent) -> bool:
    """Immediate fills for cycle second-leg reduce-only MARKET fallbacks without price/trigger."""
    order_type = str(intent.order_type or "Market").strip().upper()
    if order_type != "MARKET":
        return False
    if intent.price is not None or intent.trigger_price is not None:
        return False
    if not bool(getattr(intent, "reduce_only", False)):
        return False
    return _is_cycle_second_leg_reduce_purpose(str(intent.purpose or ""))


def is_immediate_market_fill(intent: StrategyIntent) -> bool:
    """Initial hedge entries and immediate market cycle/refill/reload orders."""
    purpose = str(intent.purpose or "").strip().upper()
    if purpose in INITIAL_ENTRY_PURPOSES:
        return True
    if is_stuck_recovery_reload_market_fill(intent):
        return True
    if is_immediate_refill_market_fill(intent):
        return True
    return is_immediate_cycle_second_leg_market_fill(intent)


def order_trigger_side(order: VirtualOrder) -> str:
    """Map virtual order to buy/sell trigger semantics for high/low checks."""
    side = str(order.side).lower()
    if side == "long":
        return "sell" if order.reduce_only else "buy"
    if side == "short":
        return "buy" if order.reduce_only else "sell"
    raise ValueError(f"unsupported order side for trigger check: {order.side}")


def normalize_trigger_direction(value: object) -> str | None:
    """Map strategy trigger_direction to ``rise`` or ``fall`` semantics."""
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "rise", "above", "gte"}:
            return "rise"
        if normalized in {"2", "-1", "fall", "below", "lte"}:
            return "fall"
        return "unknown"
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return "unknown"
    if numeric == 1:
        return "rise"
    if numeric in (2, -1):
        return "fall"
    return "unknown"


def evaluate_order_touch(order: VirtualOrder, candle: SyntheticCandle) -> OrderTouchResult:
    """Determine whether a resting order is touchable on a 5m OHLC candle."""
    low = float(candle.low if candle.low is not None else candle.close)
    high = float(candle.high if candle.high is not None else candle.close)

    if order.trigger_price is not None:
        trigger = float(order.trigger_price)
        fill_price = float(order.price) if order.price is not None else trigger
        direction = normalize_trigger_direction(order.trigger_direction)
        if direction == "rise":
            touched = high >= trigger
            return OrderTouchResult(
                touched=touched,
                trigger_touch_rule="high>=trigger",
                order_check_price=trigger,
                fill_price=fill_price,
            )
        if direction == "fall":
            touched = low <= trigger
            return OrderTouchResult(
                touched=touched,
                trigger_touch_rule="low<=trigger",
                order_check_price=trigger,
                fill_price=fill_price,
            )
        return OrderTouchResult(
            touched=False,
            trigger_warning="missing_trigger_direction",
            order_check_price=trigger,
            fill_price=fill_price,
        )

    if order.price is not None:
        price = float(order.price)
        side = order_trigger_side(order)
        if side == "buy":
            return OrderTouchResult(
                touched=low <= price,
                trigger_touch_rule="limit_buy_low<=price",
                order_check_price=price,
                fill_price=price,
            )
        return OrderTouchResult(
            touched=high >= price,
            trigger_touch_rule="limit_sell_high>=price",
            order_check_price=price,
            fill_price=price,
        )

    return OrderTouchResult(
        touched=False,
        trigger_warning="no_price_or_trigger",
        order_check_price=None,
        fill_price=None,
    )


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
    return evaluate_order_touch(order, candle).touched


def conservative_fill_rank_key(order: VirtualOrder) -> tuple[int, float, int, str]:
    """Rank touchable orders for adverse 5m OHLC fill ordering."""
    check_price, _ = resolve_order_check_and_fill_prices(order)
    if order.trigger_price is not None:
        direction = normalize_trigger_direction(order.trigger_direction)
        if direction == "fall":
            return (0, -float(check_price), int(order.created_index), order.order_id)
        if direction == "rise":
            return (1, float(check_price), int(order.created_index), order.order_id)
        return (2, float(check_price), int(order.created_index), order.order_id)

    side = order_trigger_side(order)
    if side == "buy":
        return (0, -float(check_price), int(order.created_index), order.order_id)
    return (1, float(check_price), int(order.created_index), order.order_id)


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
    touch_metadata: dict[str, object] | None = None,
) -> FillEvent:
    order_before = book.get_order(order_id)
    order_check_price: float | None = None
    simulated_closed_pnl: float | None = None
    simulated_pnl_details: dict[str, float | None] | None = None

    if order_before is not None:
        try:
            order_check_price, _ = resolve_order_check_and_fill_prices(order_before)
        except ValueError:
            order_check_price = float(fill_price)

        side = str(order_before.side or "").lower()
        reduce_only = bool(order_before.reduce_only)
        requested_qty = float(order_before.filled_qty or order_before.qty or 0.0)

        if side == "long":
            avg_entry_price = float(book.long_avg or 0.0)
            close_qty = min(requested_qty, float(book.long_qty or 0.0)) if reduce_only else requested_qty
        elif side == "short":
            avg_entry_price = float(book.short_avg or 0.0)
            close_qty = min(requested_qty, float(book.short_qty or 0.0)) if reduce_only else requested_qty
        else:
            avg_entry_price = 0.0
            close_qty = requested_qty

        fee_rate_raw = (order_before.metadata or {}).get("fee_rate")
        if fee_rate_raw is None:
            fee_rate_raw = (order_before.metadata or {}).get("runtime_fee_rate")
        if fee_rate_raw is None:
            fee_rate_raw = book.fee_rate
        fee_rate = float(fee_rate_raw) if fee_rate_raw is not None else None

        simulated_closed_pnl, simulated_pnl_details = closed_pnl_for_virtual_order_fill(
            side=side,
            reduce_only=reduce_only,
            avg_entry_price=avg_entry_price,
            fill_price=float(fill_price),
            qty=float(close_qty),
            fee_rate=fee_rate,
        )
    else:
        order_check_price = float(fill_price)

    order, _ = book.apply_fill(order_id=order_id, fill_price=fill_price)
    _mark_order_terminal(runtime_state, order)
    book.sync_runtime_state(runtime_state)
    fill_event = virtual_order_to_fill_event(order, fill_price=fill_price, occurred_at=occurred_at)

    if simulated_closed_pnl is not None:
        attach_closed_pnl_metadata(
            fill_event.metadata,
            simulated_closed_pnl,
            pnl_details=simulated_pnl_details,
        )

    if order_check_price is not None:
        fill_event.metadata["order_check_price"] = float(order_check_price)
    if touch_metadata:
        for key, value in touch_metadata.items():
            if value is not None:
                fill_event.metadata[key] = value
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
        purpose = str(intent.purpose or "").strip().upper()
        if purpose not in INITIAL_ENTRY_PURPOSES:
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
    return sorted(touchable, key=conservative_fill_rank_key)


def _select_paired_exit_orders(
    touchable: list[VirtualOrder],
    candle: SyntheticCandle,
    *,
    max_fills_per_candle: int,
) -> tuple[list[VirtualOrder], int]:
    """Fill up to ``max_fills_per_candle`` touchable exit orders when possible."""
    exit_orders = [order for order in touchable if is_exit_purpose(order.purpose)]
    if len(exit_orders) >= 2:
        selected = sorted(exit_orders, key=lambda item: (item.created_index, item.order_id))[
            :max_fills_per_candle
        ]
        paired_count = len(selected) if len(selected) >= 2 else 0
        return selected, paired_count

    if len(exit_orders) == 1:
        selected = list(exit_orders)
        if max_fills_per_candle > 1:
            remaining = [
                order for order in touchable if order.order_id != exit_orders[0].order_id
            ]
            extra = rank_conservative_fill_orders(remaining, candle)[
                : max(0, max_fills_per_candle - 1)
            ]
            selected.extend(extra)
        return selected[:max_fills_per_candle], 0

    ranked = rank_conservative_fill_orders(touchable, candle)
    return ranked[:max_fills_per_candle], 0


def select_orders_for_fill_model(
    eligible_orders: list[VirtualOrder],
    candle: SyntheticCandle,
    *,
    fill_model: str,
    max_fills_per_candle: int,
) -> tuple[list[VirtualOrder], dict[str, int]]:
    """Select candle-start eligible orders to fill under the requested model."""
    stats = {"paired_exit_fills_count": 0}
    if max_fills_per_candle <= 0:
        return [], stats

    touchable = [
        order for order in eligible_orders if should_fill_order_on_candle(order, candle)
    ]
    if not touchable:
        return [], stats

    model = normalize_fill_model(fill_model)
    if model == "paired_exit":
        selected, paired_count = _select_paired_exit_orders(
            touchable,
            candle,
            max_fills_per_candle=max_fills_per_candle,
        )
        stats["paired_exit_fills_count"] = paired_count
        return selected, stats

    # Even in conservative mode, basket final exits must be paired when both
    # sides touch in the same OHLC candle. Otherwise one side can close while
    # the paired final-exit order remains open, leaving an impossible hedge
    # state such as long_qty=0 with only SHORT_SL_EXIT active.
    if model in {"conservative", "conservative_multi"}:
        # Conservative normally allows only one fill per candle. Basket final
        # exits are the exception: if both final exit legs touch in the same
        # OHLC candle, both must fill together to avoid an impossible one-sided
        # hedge state.
        paired_exit_limit = max(2, int(max_fills_per_candle))
        selected, paired_count = _select_paired_exit_orders(
            touchable,
            candle,
            max_fills_per_candle=paired_exit_limit,
        )
        if paired_count >= 2:
            stats["paired_exit_fills_count"] = paired_count
            return selected, stats

    ranked = rank_conservative_fill_orders(eligible_orders, candle)
    return ranked[:max_fills_per_candle], stats


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
    eligible_orders: list[VirtualOrder] | None = None,
    fill_model: str = "conservative",
    max_fills_per_candle: int | None = None,
    conservative_fill_order: bool = True,
) -> tuple[list[FillEvent], dict[str, int]]:
    """Check eligible resting orders against candle high/low and emit FillEvents.

    Phase 7: only orders in ``eligible_orders`` (candle-start snapshot) may fill.
    New orders submitted during ``on_fill`` in the same candle are not eligible.
    Pass ``eligible_orders=None`` and ``max_fills_per_candle=None`` for legacy
    unlimited fills from all active orders (smoke tests).
    """
    stats = {"same_candle_fill_count": 0, "paired_exit_fills_count": 0}

    if eligible_orders is None:
        order_pool = list(book.active_orders())
    else:
        active_ids = {order.order_id for order in book.active_orders()}
        order_pool = [order for order in eligible_orders if order.order_id in active_ids]

    if max_fills_per_candle is None:
        candidates = [order for order in order_pool if should_fill_order_on_candle(order, candle)]
    elif max_fills_per_candle <= 0:
        return [], stats
    elif conservative_fill_order and fill_model in {"conservative", "conservative_multi", "paired_exit"}:
        candidates, model_stats = select_orders_for_fill_model(
            order_pool,
            candle,
            fill_model=fill_model,
            max_fills_per_candle=int(max_fills_per_candle),
        )
        stats.update(model_stats)
    else:
        candidates = [
            order
            for order in sorted(order_pool, key=lambda item: (item.created_index, item.order_id))
            if should_fill_order_on_candle(order, candle)
        ][: int(max_fills_per_candle)]

    fill_events: list[FillEvent] = []
    for order in candidates:
        touch = evaluate_order_touch(order, candle)
        fill_price = float(touch.fill_price if touch.fill_price is not None else order.trigger_price or order.price or candle.close)
        fill_events.append(
            fill_order_at_price(
                book=book,
                runtime_state=runtime_state,
                order_id=order.order_id,
                fill_price=fill_price,
                occurred_at=candle.timestamp,
                touch_metadata={
                    "trigger_touched": touch.touched,
                    "trigger_touch_rule": touch.trigger_touch_rule,
                    "trigger_warning": touch.trigger_warning,
                    "order_check_price": touch.order_check_price,
                },
            )
        )

    stats["same_candle_fill_count"] = len(fill_events)
    return fill_events, stats


# Backward-compatible aliases for Phase-1 callers.
fill_intent_at_candle_close = fill_order_at_candle_close
fill_intents_at_candle_close = fill_entry_intents_at_candle_close

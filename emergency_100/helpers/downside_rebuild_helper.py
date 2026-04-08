from __future__ import annotations

from typing import TYPE_CHECKING, Any

from strategy.execution.order_executor import OrderIntent

if TYPE_CHECKING:
    from emergency_100.final_hedge_strategy import PSRHStrategy


def calculate_paired_partial_sl_long_trigger(
    strategy: PSRHStrategy, short_fill_price: float
) -> float:
    spread_pct_decimal = strategy._calculate_doc_spread_pct() / 100.0
    buffer_pct = max(0.0, strategy.config.paired_partial_sl_long_buffer_pct)
    return short_fill_price * (1 + spread_pct_decimal + buffer_pct)


def build_paired_partial_sl_long_intent_from_filled_short(
    strategy: PSRHStrategy,
    client_order_id: str,
    order: dict[str, Any],
) -> OrderIntent | None:
    long_size, _, _, _ = strategy._get_position_snapshot()
    fill_qty = float(order.get("filled_qty") or order.get("qty") or order.get("size") or 0.0)
    metadata = order.get("metadata") or {}
    fill_price = float(
        metadata.get("last_fill_price")
        or order.get("price")
        or 0.0
    )
    if long_size <= 0 or fill_qty <= 0 or fill_price <= 0:
        strategy.logger.debug(
            "Paired partial SL long skipped",
            extra={
                "event": "rebuild_skipped",
                "purpose": "paired_partial_sl_long",
                "side": "long",
                "state": strategy.state_machine.state.value,
                "client_order_id": client_order_id,
                "long_size": long_size,
                "raw_qty": fill_qty,
                "price": fill_price,
                "reason": "invalid_fill_context",
                "result": "skipped",
            },
        )
        return None
    if fill_qty <= 0:
        return None
    trigger_price = calculate_paired_partial_sl_long_trigger(strategy, fill_price)
    strategy.logger.debug(
        "Paired partial SL long trigger calculated",
        extra={
            "event": "rebuild_triggered",
            "purpose": "paired_partial_sl_long",
            "side": "long",
            "state": strategy.state_machine.state.value,
            "client_order_id": client_order_id,
            "long_size": long_size,
            "price": fill_price,
            "raw_qty": fill_qty,
            "trigger_price": trigger_price,
            "result": "calculating",
        },
    )
    metadata = {
        "paired_partial_sl_long": True,
        "linked_short_heal_order_id": client_order_id,
        "linked_short_fill_price": fill_price,
        "linked_short_fill_qty": fill_qty,
        "trigger_price": trigger_price,
        "reduce_only": True,
        "order_type": "Limit",
    }
    intent = OrderIntent(
        side="long",
        qty=fill_qty,
        price=trigger_price,
        purpose="paired_partial_sl_long",
        reduce_only=True,
        order_type="Limit",
        metadata=metadata,
    )
    strategy.logger.info(
        "Paired partial SL long intent prepared",
        extra={
            "event": "order_intent_prepared",
            "purpose": intent.purpose,
            "side": intent.side,
            "state": strategy.state_machine.state.value,
            "client_order_id": client_order_id,
            "price": trigger_price,
            "raw_qty": fill_qty,
            "normalized_qty": intent.qty,
            "reduce_only": intent.reduce_only,
            "order_type": intent.order_type,
            "result": "prepared",
        },
    )
    return intent


def handle_filled_spread_heal_short(
    strategy: PSRHStrategy, client_order_id: str, order: dict[str, Any], source: str
) -> None:
    with strategy._order_lock:
        current_order = strategy.active_orders.get(client_order_id)
        if not current_order:
            return
        metadata = current_order.setdefault("metadata", {})
        if metadata.get("paired_partial_sl_long_created", False):
            return
        metadata["paired_partial_sl_long_created"] = "pending"
    strategy.sync_positions_with_exchange()
    paired_intent = build_paired_partial_sl_long_intent_from_filled_short(
        strategy,
        client_order_id,
        order,
    )
    if not paired_intent:
        with strategy._order_lock:
            current_order = strategy.active_orders.get(client_order_id)
            if current_order:
                current_order.setdefault("metadata", {})["paired_partial_sl_long_created"] = False
        strategy.logger.info(
            "Filled short heal produced no paired partial SL long",
        extra={
            "event": "rebuild_skipped",
            "client_order_id": client_order_id,
            "purpose": "paired_partial_sl_long",
            "side": "long",
            "source": source,
            "reason": "paired_intent_not_built",
            "result": "skipped",
        },
        )
        return
    with strategy._order_lock:
        current_order = strategy.active_orders.get(client_order_id)
        if not current_order:
            return
        current_order.setdefault("metadata", {})["paired_partial_sl_long_created"] = True
    strategy.logger.info(
        "Filled short heal -> spawning paired partial SL long",
        extra={
            "event": "rebuild_triggered",
            "client_order_id": client_order_id,
            "source": source,
            "paired_trigger_price": paired_intent.price,
            "paired_qty": paired_intent.qty,
            "purpose": paired_intent.purpose,
            "side": paired_intent.side,
            "result": "spawning_paired_close",
        },
    )
    strategy._execute_intents([paired_intent])


def cancel_future_open_short_heal_orders(strategy: PSRHStrategy) -> None:
    active_statuses = {"PENDING_SUBMIT", "OPEN", "UNKNOWN", "PARTIAL"}
    with strategy._order_lock:
        candidate_ids = [
            client_id
            for client_id, order in strategy.active_orders.items()
            if strategy._is_spread_heal_short_order(order)
            and bool((order.get("metadata") or {}).get("future_short_heal", False))
            and order.get("status") in active_statuses
        ]
    for client_id in candidate_ids:
        strategy._cancel_order_by_client_id(
            client_id,
            "paired_long_close_rebuild_short_heals",
        )


def rebuild_future_short_heals_from_current_short_size(strategy: PSRHStrategy) -> None:
    long_size, short_size, long_avg, short_avg = strategy._get_position_snapshot()
    if long_size <= 0 or long_avg <= 0:
        strategy.logger.debug(
            "Future short heal rebuild skipped",
            extra={
                "event": "rebuild_skipped",
                "purpose": "spread_heal_short",
                "side": "short",
                "state": strategy.state_machine.state.value,
                "long_size": long_size,
                "short_size": short_size,
                "long_avg": long_avg,
                "short_avg": short_avg,
                "reason": "invalid_snapshot",
                "result": "skipped",
            },
        )
        return
    next_short_qty = short_size * strategy._fine_heal_size_pct()
    strategy.logger.debug(
        "Future short heal rebuild qty calculated",
        extra={
            "event": "spread_heal_short_qty_calculated",
            "purpose": "spread_heal_short",
            "side": "short",
            "state": strategy.state_machine.state.value,
            "long_size": long_size,
            "short_size": short_size,
            "long_avg": long_avg,
            "short_avg": short_avg,
            "raw_qty": next_short_qty,
            "result": "calculated",
        },
    )
    normalized_qty = strategy._normalize_order_qty(next_short_qty, "SPREAD_HEAL_SHORT")
    if normalized_qty <= 0:
        strategy.logger.debug(
            "Future short heal rebuild skipped",
            extra={
                "event": "rebuild_skipped",
                "purpose": "spread_heal_short",
                "side": "short",
                "state": strategy.state_machine.state.value,
                "price": 0.0,
                "qty": normalized_qty,
                "reason": "normalized_qty_zero",
                "result": "skipped",
            },
        )
        return
    _, short_heal_price = strategy._compute_preplaced_heal_prices(long_avg, short_avg)
    if short_heal_price <= 0:
        strategy.logger.debug(
            "Future short heal rebuild skipped",
            extra={
                "event": "rebuild_skipped",
                "purpose": "spread_heal_short",
                "side": "short",
                "state": strategy.state_machine.state.value,
                "price": short_heal_price,
                "qty": normalized_qty,
                "reason": "invalid_heal_price",
                "result": "skipped",
            },
        )
        return
    if not strategy._meets_min_order_value(
        short_heal_price,
        normalized_qty,
        "SPREAD_HEAL_SHORT",
    ):
        strategy.logger.debug(
            "Future short heal rebuild skipped",
            extra={
                "event": "rebuild_skipped",
                "purpose": "spread_heal_short",
                "side": "short",
                "state": strategy.state_machine.state.value,
                "price": short_heal_price,
                "qty": normalized_qty,
                "reason": "below_min_order_value",
                "result": "skipped",
            },
        )
        return
    intent = OrderIntent(
        side="short",
        qty=normalized_qty,
        price=short_heal_price,
        purpose="spread_heal_short",
        reduce_only=True,
        order_type="Limit",
        metadata={
            "future_short_heal": True,
            "rebuilt_from_long_close": True,
            "based_on_short_size": short_size,
            "reduce_only": True,
            "order_type": "Limit",
        },
    )
    strategy.logger.info(
        "Rebuilding future short heal from current short size",
        extra={
            "event": "rebuild_triggered",
            "purpose": intent.purpose,
            "side": intent.side,
            "state": strategy.state_machine.state.value,
            "current_short_size": short_size,
            "next_short_qty": normalized_qty,
            "short_heal_price": short_heal_price,
            "reduce_only": intent.reduce_only,
            "order_type": intent.order_type,
            "result": "prepared",
        },
    )
    strategy._execute_intents([intent])


def handle_filled_paired_long_close(
    strategy: PSRHStrategy, client_order_id: str, order: dict[str, Any], source: str
) -> None:
    with strategy._order_lock:
        current_order = strategy.active_orders.get(client_order_id)
        if not current_order:
            return
        metadata = current_order.setdefault("metadata", {})
        if metadata.get("future_short_heal_rebuild_handled") is True:
            return
        metadata["future_short_heal_rebuild_handled"] = True
    strategy.logger.info(
        "Paired partial SL long filled -> rebuilding future short heals",
        extra={
            "event": "fill_processed",
            "client_order_id": client_order_id,
            "purpose": "paired_partial_sl_long",
            "side": "long",
            "source": source,
            "result": "rebuild_triggered",
        },
    )
    strategy.sync_positions_with_exchange()
    cancel_future_open_short_heal_orders(strategy)
    rebuild_future_short_heals_from_current_short_size(strategy)

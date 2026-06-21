from __future__ import annotations

from typing import TYPE_CHECKING, Any

from strategy.execution.order_executor import OrderIntent

if TYPE_CHECKING:
    from emergency_100.final_hedge_strategy import PSRHStrategy


def calculate_paired_partial_sl_short_trigger(
    strategy: PSRHStrategy, long_fill_price: float
) -> float:
    spread_pct_decimal = strategy._calculate_doc_spread_pct() / 100.0
    buffer_pct = max(0.0, strategy.config.paired_partial_sl_long_buffer_pct)
    return long_fill_price * (1 - spread_pct_decimal - buffer_pct)


def build_paired_partial_sl_short_intent_from_filled_long(
    strategy: PSRHStrategy,
    client_order_id: str,
    order: dict[str, Any],
) -> OrderIntent | None:
    _, short_size, _, _ = strategy._get_position_snapshot()
    fill_qty = float(order.get("filled_qty") or order.get("qty") or order.get("size") or 0.0)
    metadata = order.get("metadata") or {}
    fill_price = float(
        metadata.get("last_fill_price")
        or order.get("price")
        or 0.0
    )
    if short_size <= 0 or fill_qty <= 0 or fill_price <= 0:
        strategy.logger.debug(
            "Paired partial SL short skipped",
            extra={
                "event": "rebuild_skipped",
                "purpose": "paired_partial_sl_short",
                "side": "short",
                "state": strategy.state_machine.state.value,
                "client_order_id": client_order_id,
                "short_size": short_size,
                "raw_qty": fill_qty,
                "price": fill_price,
                "reason": "invalid_fill_context",
                "result": "skipped",
            },
        )
        return None
    trigger_price = calculate_paired_partial_sl_short_trigger(strategy, fill_price)
    strategy.logger.debug(
        "Paired partial SL short trigger calculated",
        extra={
            "event": "rebuild_triggered",
            "purpose": "paired_partial_sl_short",
            "side": "short",
            "state": strategy.state_machine.state.value,
            "client_order_id": client_order_id,
            "short_size": short_size,
            "price": fill_price,
            "raw_qty": fill_qty,
            "trigger_price": trigger_price,
            "result": "calculating",
        },
    )
    metadata = {
        "paired_partial_sl_short": True,
        "linked_long_heal_order_id": client_order_id,
        "linked_long_fill_price": fill_price,
        "linked_long_fill_qty": fill_qty,
        "trigger_price": trigger_price,
        "reduce_only": True,
        "order_type": "Limit",
    }
    intent = OrderIntent(
        side="short",
        qty=fill_qty,
        price=trigger_price,
        purpose="paired_partial_sl_short",
        reduce_only=True,
        order_type="Limit",
        metadata=metadata,
    )
    strategy.logger.info(
        "Paired partial SL short intent prepared",
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


def handle_filled_spread_heal_long(
    strategy: PSRHStrategy, client_order_id: str, order: dict[str, Any], source: str
) -> None:
    with strategy._order_lock:
        current_order = strategy.active_orders.get(client_order_id)
        if not current_order:
            return
        metadata = current_order.setdefault("metadata", {})
        if metadata.get("paired_partial_sl_short_created", False):
            return
        metadata["paired_partial_sl_short_created"] = "pending"
    strategy.sync_positions_with_exchange()
    paired_intent = build_paired_partial_sl_short_intent_from_filled_long(
        strategy,
        client_order_id,
        order,
    )
    if not paired_intent:
        with strategy._order_lock:
            current_order = strategy.active_orders.get(client_order_id)
            if current_order:
                current_order.setdefault("metadata", {})["paired_partial_sl_short_created"] = False
        strategy.logger.info(
            "Filled long heal produced no paired partial SL short",
        extra={
            "event": "rebuild_skipped",
            "client_order_id": client_order_id,
            "purpose": "paired_partial_sl_short",
            "side": "short",
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
        current_order.setdefault("metadata", {})["paired_partial_sl_short_created"] = True
    strategy.logger.info(
        "Filled long heal -> spawning paired partial SL short",
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


def cancel_future_open_long_heal_orders(strategy: PSRHStrategy) -> None:
    active_statuses = {"PENDING_SUBMIT", "OPEN", "UNKNOWN", "PARTIAL"}
    with strategy._order_lock:
        candidate_ids = [
            client_id
            for client_id, order in strategy.active_orders.items()
            if strategy._is_spread_heal_long_order(order)
            and bool((order.get("metadata") or {}).get("future_long_heal", False))
            and order.get("status") in active_statuses
        ]
    for client_id in candidate_ids:
        strategy._cancel_order_by_client_id(
            client_id,
            "paired_short_close_rebuild_long_heals",
        )


def rebuild_future_long_heals_from_current_long_size(strategy: PSRHStrategy) -> None:
    long_size, short_size, long_avg, short_avg = strategy._get_position_snapshot()
    if short_size <= 0 or short_avg <= 0:
        strategy.logger.debug(
            "Future long heal rebuild skipped",
            extra={
                "event": "rebuild_skipped",
                "purpose": "spread_heal_long",
                "side": "long",
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
    next_long_qty = long_size * strategy._fine_heal_size_pct()
    strategy.logger.debug(
        "Future long heal rebuild qty calculated",
        extra={
            "event": "spread_heal_long_qty_calculated",
            "purpose": "spread_heal_long",
            "side": "long",
            "state": strategy.state_machine.state.value,
            "long_size": long_size,
            "short_size": short_size,
            "long_avg": long_avg,
            "short_avg": short_avg,
            "raw_qty": next_long_qty,
            "result": "calculated",
        },
    )
    normalized_qty = strategy._normalize_order_qty(next_long_qty, "SPREAD_HEAL_LONG")
    if normalized_qty <= 0:
        strategy.logger.debug(
            "Future long heal rebuild skipped",
            extra={
                "event": "rebuild_skipped",
                "purpose": "spread_heal_long",
                "side": "long",
                "state": strategy.state_machine.state.value,
                "price": 0.0,
                "qty": normalized_qty,
                "reason": "normalized_qty_zero",
                "result": "skipped",
            },
        )
        return
    long_heal_price, _ = strategy._compute_preplaced_heal_prices(long_avg, short_avg)
    if long_heal_price <= 0:
        strategy.logger.debug(
            "Future long heal rebuild skipped",
            extra={
                "event": "rebuild_skipped",
                "purpose": "spread_heal_long",
                "side": "long",
                "state": strategy.state_machine.state.value,
                "price": long_heal_price,
                "qty": normalized_qty,
                "reason": "invalid_heal_price",
                "result": "skipped",
            },
        )
        return
    if not strategy._meets_min_order_value(
        long_heal_price,
        normalized_qty,
        "SPREAD_HEAL_LONG",
    ):
        strategy.logger.debug(
            "Future long heal rebuild skipped",
            extra={
                "event": "rebuild_skipped",
                "purpose": "spread_heal_long",
                "side": "long",
                "state": strategy.state_machine.state.value,
                "price": long_heal_price,
                "qty": normalized_qty,
                "reason": "below_min_order_value",
                "result": "skipped",
            },
        )
        return
    intent = OrderIntent(
        side="long",
        qty=normalized_qty,
        price=long_heal_price,
        purpose="spread_heal_long",
        reduce_only=False,
        order_type="Limit",
        metadata={
            "future_long_heal": True,
            "rebuilt_from_short_close": True,
            "based_on_long_size": long_size,
            "order_type": "Limit",
        },
    )
    strategy.logger.info(
        "Rebuilding future long heal from current long size",
        extra={
            "event": "rebuild_triggered",
            "purpose": intent.purpose,
            "side": intent.side,
            "state": strategy.state_machine.state.value,
            "current_long_size": long_size,
            "next_long_qty": normalized_qty,
            "long_heal_price": long_heal_price,
            "reduce_only": intent.reduce_only,
            "order_type": intent.order_type,
            "result": "prepared",
        },
    )
    strategy._execute_intents([intent])


def handle_filled_paired_short_close(
    strategy: PSRHStrategy, client_order_id: str, order: dict[str, Any], source: str
) -> None:
    with strategy._order_lock:
        current_order = strategy.active_orders.get(client_order_id)
        if not current_order:
            return
        metadata = current_order.setdefault("metadata", {})
        if metadata.get("future_long_heal_rebuild_handled") is True:
            return
        metadata["future_long_heal_rebuild_handled"] = True
    strategy.logger.info(
        "Paired partial SL short filled -> rebuilding future long heals",
        extra={
            "event": "fill_processed",
            "client_order_id": client_order_id,
            "purpose": "paired_partial_sl_short",
            "side": "short",
            "source": source,
            "result": "rebuild_triggered",
        },
    )
    strategy.sync_positions_with_exchange()
    cancel_future_open_long_heal_orders(strategy)
    rebuild_future_long_heals_from_current_long_size(strategy)

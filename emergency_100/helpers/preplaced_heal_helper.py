from __future__ import annotations

from typing import TYPE_CHECKING, Any

from strategy.execution.order_executor import OrderIntent

if TYPE_CHECKING:
    from emergency_100.final_hedge_strategy import PSRHStrategy


def compute_preplaced_heal_prices(
    strategy: PSRHStrategy, long_avg: float, short_avg: float
) -> tuple[float, float]:
    offset = max(0.0, strategy.config.preplaced_heal_offset_pct)
    long_heal_price = short_avg * (1 - offset)
    short_heal_price = long_avg * (1 + offset)
    return long_heal_price, short_heal_price


def preplaced_heal_mode_active(
    strategy: PSRHStrategy, *, spread_pct: float | None = None
) -> bool:
    if not strategy.config.preplaced_heal_enabled:
        strategy.logger.debug(
            "Preplaced heal mode inactive",
            extra={
                "event": "preplaced_heal_arm_evaluated",
                "state": strategy.state_machine.state.value,
                "reason": "feature_disabled",
                "result": False,
            },
        )
        return False
    if not strategy._is_preplaced_heal_state():
        strategy.logger.debug(
            "Preplaced heal mode inactive",
            extra={
                "event": "preplaced_heal_arm_evaluated",
                "state": strategy.state_machine.state.value,
                "reason": "state_not_eligible",
                "result": False,
            },
        )
        return False
    long_size, short_size, long_avg, short_avg = strategy._get_position_snapshot()
    if min(long_size, short_size, long_avg, short_avg) <= 0:
        strategy.logger.debug(
            "Preplaced heal mode inactive",
            extra={
                "event": "preplaced_heal_arm_evaluated",
                "state": strategy.state_machine.state.value,
                "long_size": long_size,
                "short_size": short_size,
                "long_avg": long_avg,
                "short_avg": short_avg,
                "reason": "invalid_snapshot",
                "result": False,
            },
        )
        return False
    current_spread_pct = (
        strategy._calculate_doc_spread_pct() if spread_pct is None else spread_pct
    )
    active = current_spread_pct > strategy.config.spread_heal_trigger_pct
    strategy.logger.debug(
        "Preplaced heal mode evaluated",
        extra={
            "event": "preplaced_heal_arm_evaluated",
            "state": strategy.state_machine.state.value,
            "long_size": long_size,
            "short_size": short_size,
            "long_avg": long_avg,
            "short_avg": short_avg,
            "spread_pct": current_spread_pct,
            "reason": "spread_check",
            "result": active,
        },
    )
    return active


def should_arm_preplaced_heal_orders(strategy: PSRHStrategy) -> bool:
    if not strategy._preplaced_heal_mode_active():
        return False
    if strategy._preplaced_heal_rearm_in_progress:
        strategy.logger.debug(
            "Preplaced heal arming skipped",
            extra={
                "event": "preplaced_heal_arm_evaluated",
                "state": strategy.state_machine.state.value,
                "reason": "rearm_in_progress",
                "result": False,
            },
        )
        return False
    if strategy._preplaced_heal_orders_armed and not strategy._has_active_preplaced_heal_orders():
        strategy._preplaced_heal_orders_armed = False
        strategy._active_preplaced_heal_long_client_id = None
        strategy._active_preplaced_heal_short_client_id = None
    if strategy._preplaced_heal_orders_armed:
        strategy.logger.debug(
            "Preplaced heal arming skipped",
            extra={
                "event": "preplaced_heal_arm_evaluated",
                "state": strategy.state_machine.state.value,
                "reason": "already_armed",
                "result": False,
            },
        )
        return False
    strategy.logger.debug(
        "Preplaced heal arming allowed",
        extra={
            "event": "preplaced_heal_arm_evaluated",
            "state": strategy.state_machine.state.value,
            "reason": "eligible",
            "result": True,
        },
    )
    return True


def build_preplaced_heal_limit_intents(
    strategy: PSRHStrategy,
) -> tuple[OrderIntent, OrderIntent] | None:
    long_size, short_size, long_avg, short_avg = strategy._get_position_snapshot()
    state = strategy.state_machine.state.value
    if min(long_size, short_size, long_avg, short_avg) <= 0:
        return None
    if strategy._long_heal_adds_remaining() <= 0 or strategy._short_heal_adds_remaining() <= 0:
        strategy.logger.info(
            "Preplaced heal arming skipped: add limit reached",
            extra={
                "long_heal_remaining": strategy._long_heal_adds_remaining(),
                "short_heal_remaining": strategy._short_heal_adds_remaining(),
            },
        )
        return None
    long_heal_price, short_heal_price = strategy._compute_preplaced_heal_prices(
        long_avg, short_avg
    )
    long_qty = strategy._long_heal_add_qty()
    short_qty = strategy._short_heal_add_qty()
    strategy.logger.debug(
        "Preplaced heal quantities calculated",
        extra={
            "event": "preplaced_heal_arm_evaluated",
            "state": state,
            "long_size": long_size,
            "short_size": short_size,
            "long_avg": long_avg,
            "short_avg": short_avg,
            "raw_long_qty": long_qty,
            "raw_short_qty": short_qty,
            "long_heal_price": long_heal_price,
            "short_heal_price": short_heal_price,
            "result": "calculating",
        },
    )
    if long_qty <= 0 or short_qty <= 0:
        return None
    long_qty = strategy._normalize_order_qty(long_qty, "PREPLACED_HEAL_LONG_LIMIT")
    short_qty = strategy._normalize_order_qty(short_qty, "PREPLACED_HEAL_SHORT_LIMIT")
    if long_qty <= 0 or short_qty <= 0:
        return None
    if not strategy._meets_min_order_value(
        long_heal_price,
        long_qty,
        "PREPLACED_HEAL_LONG_LIMIT",
    ):
        return None
    if not strategy._meets_min_order_value(
        short_heal_price,
        short_qty,
        "PREPLACED_HEAL_SHORT_LIMIT",
    ):
        return None
    if not strategy._long_heal_improves_avg(long_size, long_avg, long_heal_price, long_qty):
        strategy.logger.info(
            "Preplaced long heal skipped: avg would not improve",
            extra={"long_avg": long_avg, "long_heal_price": long_heal_price},
        )
        return None
    if not strategy._short_heal_improves_avg(
        short_size, short_avg, short_heal_price, short_qty
    ):
        strategy.logger.info(
            "Preplaced short heal skipped: avg would not improve",
            extra={"short_avg": short_avg, "short_heal_price": short_heal_price},
        )
        return None
    generation = strategy._preplaced_heal_generation
    long_metadata = {
        "preplaced_heal": True,
        "heal_side": "long",
        "heal_generation": generation,
        "paired_client_order_id": None,
        "reference_long_avg": long_avg,
        "reference_short_avg": short_avg,
    }
    short_metadata = {
        "preplaced_heal": True,
        "heal_side": "short",
        "heal_generation": generation,
        "paired_client_order_id": None,
        "reference_long_avg": long_avg,
        "reference_short_avg": short_avg,
    }
    long_intent = OrderIntent(
            side="long",
            qty=long_qty,
            price=long_heal_price,
            purpose="preplaced_heal_long_limit",
            reduce_only=False,
            order_type="Limit",
            metadata=long_metadata,
        )
    short_intent = OrderIntent(
            side="short",
            qty=short_qty,
            price=short_heal_price,
            purpose="preplaced_heal_short_limit",
            reduce_only=False,
            order_type="Limit",
            metadata=short_metadata,
        )
    strategy.logger.info(
        "Preplaced heal intents prepared",
        extra={
            "event": "order_intent_prepared",
            "state": state,
            "long_size": long_size,
            "short_size": short_size,
            "long_avg": long_avg,
            "short_avg": short_avg,
            "long_purpose": long_intent.purpose,
            "short_purpose": short_intent.purpose,
            "long_price": long_intent.price,
            "short_price": short_intent.price,
            "long_qty": long_intent.qty,
            "short_qty": short_intent.qty,
            "result": "prepared",
        },
    )
    return (long_intent, short_intent)


def collect_preplaced_heal_order_ids(
    strategy: PSRHStrategy, generation: int
) -> tuple[str | None, str | None]:
    active_statuses = {"PENDING_SUBMIT", "OPEN", "PARTIAL", "UNKNOWN"}
    long_client_id: str | None = None
    short_client_id: str | None = None
    active_orders = getattr(strategy, "active_orders", {})
    with strategy._order_lock:
        for client_id, order in active_orders.items():
            metadata = order.get("metadata") or {}
            if not metadata.get("preplaced_heal"):
                continue
            if int(metadata.get("heal_generation", -1)) != generation:
                continue
            if order.get("status") not in active_statuses:
                continue
            heal_side = (metadata.get("heal_side") or "").lower()
            if heal_side == "long":
                long_client_id = client_id
            elif heal_side == "short":
                short_client_id = client_id
    return long_client_id, short_client_id


def arm_preplaced_heal_orders(strategy: PSRHStrategy) -> bool:
    if not strategy._should_arm_preplaced_heal_orders():
        strategy.logger.debug(
            "Preplaced heal arming skipped",
            extra={
                "event": "preplaced_heal_arm_result",
                "state": strategy.state_machine.state.value,
                "reason": "arming_not_allowed",
                "result": False,
            },
        )
        return False
    intents = strategy._build_preplaced_heal_limit_intents()
    if not intents:
        strategy.logger.info(
            "Preplaced heal arming produced no intents",
            extra={
                "event": "preplaced_heal_arm_result",
                "state": strategy.state_machine.state.value,
                "reason": "no_intents_built",
                "result": False,
            },
        )
        strategy._clear_preplaced_heal_state()
        return False
    generation = strategy._preplaced_heal_generation
    strategy._execute_intents(list(intents))
    long_client_id, short_client_id = strategy._collect_preplaced_heal_order_ids(generation)
    if not long_client_id or not short_client_id:
        strategy.logger.warning(
            "Preplaced heal arming incomplete",
            extra={
                "event": "preplaced_heal_arm_result",
                "generation": generation,
                "long_client_id": long_client_id,
                "short_client_id": short_client_id,
                "result": False,
            },
        )
        strategy._cancel_preplaced_heal_orders(
            "incomplete_preplaced_heal_arm",
            long_client_id=long_client_id,
            short_client_id=short_client_id,
        )
        return False
    with strategy._order_lock:
        active_orders = getattr(strategy, "active_orders", {})
        long_order = active_orders.get(long_client_id)
        short_order = active_orders.get(short_client_id)
        if long_order:
            long_order.setdefault("metadata", {})["paired_client_order_id"] = short_client_id
        if short_order:
            short_order.setdefault("metadata", {})["paired_client_order_id"] = long_client_id
    strategy._active_preplaced_heal_long_client_id = long_client_id
    strategy._active_preplaced_heal_short_client_id = short_client_id
    strategy._preplaced_heal_orders_armed = True
    strategy.logger.info(
        "Preplaced heal orders armed",
        extra={
            "event": "preplaced_heal_arm_result",
            "generation": generation,
            "long_client_id": long_client_id,
            "short_client_id": short_client_id,
            "result": True,
        },
    )
    return True


def cancel_order_by_client_id(
    strategy: PSRHStrategy, client_id: str, reason: str
) -> bool:
    from emergency_100.final_hedge_strategy import _utcnow

    active_orders = getattr(strategy, "active_orders", {})
    with strategy._order_lock:
        order = active_orders.get(client_id)
        if not order:
            return False
        if order.get("status") in {"FILLED", "FILLED_HANDLED", "CANCELED"}:
            return False
        exchange_order_id = order.get("exchange_order_id")
        purpose = order.get("purpose")
        side = order.get("side")
    cancelled = False
    if strategy.order_manager and exchange_order_id:
        try:
            cancelled = bool(
                strategy.order_manager.cancel_order(
                    exchange_order_id,
                    symbol=strategy.config.default_symbol,
                    category=strategy.config.category,
                )
            )
        except Exception as exc:
            strategy.logger.warning(
                "Failed to cancel preplaced heal order",
                extra={
                    "event": "preplaced_heal_cancel_result",
                    "client_order_id": client_id,
                    "exchange_order_id": exchange_order_id,
                    "purpose": purpose,
                    "reason": reason,
                    "error": str(exc),
                    "result": False,
                },
            )
            return False
    else:
        cancelled = True
    if not cancelled:
        strategy.logger.warning(
            "Exchange declined preplaced heal order cancellation",
            extra={
                "event": "preplaced_heal_cancel_result",
                "client_order_id": client_id,
                "exchange_order_id": exchange_order_id,
                "purpose": purpose,
                "reason": reason,
                "result": False,
            },
        )
        return False
    with strategy._order_lock:
        active_orders = getattr(strategy, "active_orders", {})
        order = active_orders.get(client_id)
        if order:
            order["status"] = "CANCELED"
            order["updated_at"] = _utcnow()
            strategy._handle_order_finalized_locked(client_id, order)
    strategy.logger.info(
        "Canceled preplaced heal order",
        extra={
            "event": "preplaced_heal_cancel_result",
            "client_order_id": client_id,
            "exchange_order_id": exchange_order_id,
            "purpose": purpose,
            "side": side,
            "reason": reason,
            "result": True,
        },
    )
    return True


def cancel_preplaced_heal_orders(
    strategy: PSRHStrategy,
    reason: str,
    *,
    exclude_client_order_id: str | None = None,
    long_client_id: str | None = None,
    short_client_id: str | None = None,
) -> None:
    candidate_ids: list[str] = []
    if long_client_id:
        candidate_ids.append(long_client_id)
    if short_client_id:
        candidate_ids.append(short_client_id)
    if not candidate_ids:
        active_orders = getattr(strategy, "active_orders", {})
        with strategy._order_lock:
            candidate_ids = [
                client_id
                for client_id, order in active_orders.items()
                if strategy._is_preplaced_heal_purpose(order.get("purpose"))
            ]
    seen: set[str] = set()
    for client_id in candidate_ids:
        if (
            not client_id
            or client_id == exclude_client_order_id
            or client_id in seen
        ):
            continue
        seen.add(client_id)
        strategy._cancel_order_by_client_id(client_id, reason)
    strategy._clear_preplaced_heal_state(reset_generation=True)


def cancel_recovered_preplaced_heal_orders(
    strategy: PSRHStrategy, reason: str
) -> None:
    active_orders = getattr(strategy, "active_orders", {})
    with strategy._order_lock:
        recovered_ids = [
            client_id
            for client_id, order in active_orders.items()
            if strategy._is_preplaced_heal_purpose(order.get("purpose"))
            and (order.get("metadata") or {}).get("recovered_from_exchange", False)
        ]
    if not recovered_ids:
        return
    strategy.logger.info(
        "Recovered preplaced heal orders detected, canceling",
        extra={
            "event": "preplaced_heal_cancel_result",
            "reason": reason,
            "client_order_ids": recovered_ids,
            "result": "canceling_recovered_orders",
        },
    )
    strategy._cancel_preplaced_heal_orders(reason)


def handle_preplaced_heal_fill(
    strategy: PSRHStrategy,
    client_order_id: str,
    purpose: str | None,
    source: str,
) -> None:
    if not strategy._is_preplaced_heal_purpose(purpose):
        return
    active_orders = getattr(strategy, "active_orders", {})
    with strategy._order_lock:
        order = active_orders.get(client_order_id)
        metadata = (order or {}).get("metadata") or {}
        paired_client_order_id = metadata.get("paired_client_order_id")
    strategy.logger.info(
        "Ignoring legacy preplaced heal fill for new strategy",
        extra={
            "event": "follow_up_processed",
            "client_order_id": client_order_id,
            "purpose": purpose,
            "source": source,
            "paired_client_order_id": paired_client_order_id,
            "reason": "legacy_preplaced_disabled",
            "result": "processed",
        },
    )
    if paired_client_order_id and paired_client_order_id != client_order_id:
        strategy._cancel_order_by_client_id(
            paired_client_order_id,
            "legacy_preplaced_disabled",
        )
    strategy._cancel_order_by_client_id(client_order_id, "legacy_preplaced_disabled")
    strategy._clear_preplaced_heal_state(reset_generation=True)

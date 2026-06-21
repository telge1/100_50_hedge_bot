from __future__ import annotations

from typing import TYPE_CHECKING, Any

from strategy.execution.order_executor import OrderIntent

if TYPE_CHECKING:
    from emergency_100.final_hedge_strategy import PSRHStrategy


def ensure_phase3_long_target_reference(strategy: PSRHStrategy) -> None:
    long_size, short_size, _, _ = strategy._get_position_snapshot()
    if long_size <= 0 or short_size <= 0:
        return
    if (
        strategy._phase3_long_target_reference_size is None
        and strategy._aggressive_down_heal_initial_short_size is None
    ):
        strategy._phase3_long_target_reference_size = long_size


def phase4_short_rebuild_enabled(strategy: PSRHStrategy) -> bool:
    return bool(strategy.config.enable_phase4_short_rebuild)


def ensure_phase4_short_target_reference(strategy: PSRHStrategy) -> None:
    long_size, short_size, _, _ = strategy._get_position_snapshot()
    if long_size <= 0 or short_size <= 0:
        return
    if (
        strategy._phase4_short_target_reference_size is None
        and strategy._aggressive_down_heal_initial_short_size is None
    ):
        strategy._phase4_short_target_reference_size = short_size


def phase3_target_long_qty(strategy: PSRHStrategy) -> float:
    reference_size = strategy._phase3_long_target_reference_size or 0.0
    target_pct = max(0.0, strategy.config.long_rebuild_target_pct)
    return reference_size * target_pct


def phase3_target_reached(strategy: PSRHStrategy) -> bool:
    long_size, _, _, _ = strategy._get_position_snapshot()
    target_long_qty = strategy._phase3_target_long_qty()
    return target_long_qty <= 0 or long_size >= target_long_qty - 1e-9


def phase4_target_short_qty(strategy: PSRHStrategy) -> float:
    reference_size = strategy._phase4_short_target_reference_size or 0.0
    target_pct = max(0.0, strategy.config.short_rebuild_target_pct)
    return reference_size * target_pct


def phase4_target_reached(strategy: PSRHStrategy) -> bool:
    _, short_size, _, _ = strategy._get_position_snapshot()
    target_short_qty = strategy._phase4_target_short_qty()
    return target_short_qty <= 0 or short_size >= target_short_qty - 1e-9


def phase3_long_rebuild_ready(strategy: PSRHStrategy) -> bool:
    long_size, short_size, _, _ = strategy._get_position_snapshot()
    target_long_qty = strategy._phase3_target_long_qty()
    return bool(
        strategy._phase3_long_rebuild_enabled()
        and short_size <= 1e-9
        and (
            not strategy._aggressive_down_heal_enabled()
            or strategy._aggressive_down_heal_phase_completed
        )
        and target_long_qty > 0
        and long_size < target_long_qty - 1e-9
    )


def phase4_short_rebuild_ready(strategy: PSRHStrategy) -> bool:
    long_size, short_size, _, _ = strategy._get_position_snapshot()
    target_short_qty = strategy._phase4_target_short_qty()
    return bool(
        strategy._phase4_short_rebuild_enabled()
        and (
            not strategy._aggressive_down_heal_enabled()
            or strategy._aggressive_down_heal_phase_completed
        )
        and strategy._phase3_target_reached()
        and target_short_qty > 0
        and short_size < target_short_qty - 1e-9
    )


def phase5_fine_heal_ready(strategy: PSRHStrategy) -> bool:
    if (
        strategy._phase3_long_rebuild_enabled()
        and not strategy._phase3_target_reached()
    ):
        return False
    if (
        strategy._phase4_short_rebuild_enabled()
        and not strategy._phase4_target_reached()
    ):
        return False
    return True


def build_phase3_long_rebuild_intent(
    strategy: PSRHStrategy, price: float
) -> OrderIntent | None:
    long_size, _, long_avg, _ = strategy._get_position_snapshot()
    target_long_qty = strategy._phase3_target_long_qty()
    state = strategy.state_machine.state.value
    if long_size <= 0 or target_long_qty <= 0 or price <= 0:
        strategy.logger.debug(
            "Phase 3 long rebuild skipped",
            extra={
                "event": "rebuild_skipped",
                "purpose": "phase3_long_rebuild",
                "side": "long",
                "state": state,
                "price": price,
                "long_size": long_size,
                "long_avg": long_avg,
                "target_qty": target_long_qty,
                "reason": "invalid_position_target_or_price",
                "result": "skipped",
            },
        )
        return None
    missing_qty = max(0.0, target_long_qty - long_size)
    if missing_qty <= 0:
        strategy.logger.debug(
            "Phase 3 long rebuild skipped",
            extra={
                "event": "rebuild_skipped",
                "purpose": "phase3_long_rebuild",
                "side": "long",
                "state": state,
                "price": price,
                "long_size": long_size,
                "target_qty": target_long_qty,
                "missing_qty": missing_qty,
                "reason": "target_already_reached",
                "result": "skipped",
            },
        )
        return None
    if strategy._side_adds_remaining("long") <= 0:
        strategy.logger.info(
            "Phase 3 long rebuild skipped",
            extra={
                "event": "rebuild_skipped",
                "purpose": "phase3_long_rebuild",
                "side": "long",
                "state": state,
                "price": price,
                "long_size": long_size,
                "target_qty": target_long_qty,
                "missing_qty": missing_qty,
                "reason": "max_adds_reached",
                "result": "skipped",
            },
        )
        return None
    requested_qty = min(strategy._base_add_qty("long"), missing_qty)
    strategy.logger.debug(
        "Phase 3 long rebuild qty calculated",
        extra={
            "event": "phase3_long_rebuild_qty_calculated",
            "purpose": "phase3_long_rebuild",
            "side": "long",
            "state": state,
            "price": price,
            "long_size": long_size,
            "long_avg": long_avg,
            "target_qty": target_long_qty,
            "missing_qty": missing_qty,
            "raw_qty": requested_qty,
            "result": "calculating",
        },
    )
    normalized_qty = strategy._normalize_order_qty(requested_qty, "PHASE3_LONG_REBUILD")
    if normalized_qty <= 0:
        return None
    capped_qty = min(normalized_qty, missing_qty)
    final_qty = strategy._normalize_order_qty(capped_qty, "PHASE3_LONG_REBUILD")
    if final_qty <= 0:
        return None
    if not strategy._meets_min_order_value(price, final_qty, "PHASE3_LONG_REBUILD"):
        return None
    if not strategy._long_heal_improves_avg(long_size, long_avg, price, final_qty):
        strategy.logger.debug(
            "Phase 3 long rebuild skipped",
            extra={
                "event": "rebuild_skipped",
                "purpose": "phase3_long_rebuild",
                "side": "long",
                "state": state,
                "price": price,
                "long_size": long_size,
                "long_avg": long_avg,
                "target_qty": target_long_qty,
                "missing_qty": missing_qty,
                "normalized_qty": final_qty,
                "reason": "avg_not_improved",
                "result": "skipped",
            },
        )
        return None
    intent = OrderIntent(
        side="long",
        qty=final_qty,
        price=price,
        purpose="phase3_long_rebuild",
        order_type="Market",
    )
    strategy.logger.info(
        "Phase 3 long rebuild intent prepared",
        extra={
            "event": "order_intent_prepared",
            "purpose": intent.purpose,
            "side": intent.side,
            "state": state,
            "price": price,
            "long_size": long_size,
            "long_avg": long_avg,
            "target_qty": target_long_qty,
            "missing_qty": missing_qty,
            "raw_qty": requested_qty,
            "normalized_qty": final_qty,
            "reduce_only": False,
            "order_type": intent.order_type,
            "result": "prepared",
        },
    )
    return intent


def build_phase4_short_rebuild_intent(
    strategy: PSRHStrategy, price: float
) -> OrderIntent | None:
    _, short_size, _, short_avg = strategy._get_position_snapshot()
    target_short_qty = strategy._phase4_target_short_qty()
    state = strategy.state_machine.state.value
    if target_short_qty <= 0 or price <= 0:
        strategy.logger.debug(
            "Phase 4 short rebuild skipped",
            extra={
                "event": "rebuild_skipped",
                "purpose": "phase4_short_rebuild",
                "side": "short",
                "state": state,
                "price": price,
                "short_size": short_size,
                "short_avg": short_avg,
                "target_qty": target_short_qty,
                "reason": "invalid_target_or_price",
                "result": "skipped",
            },
        )
        return None
    missing_qty = max(0.0, target_short_qty - short_size)
    if missing_qty <= 0:
        strategy.logger.debug(
            "Phase 4 short rebuild skipped",
            extra={
                "event": "rebuild_skipped",
                "purpose": "phase4_short_rebuild",
                "side": "short",
                "state": state,
                "price": price,
                "short_size": short_size,
                "target_qty": target_short_qty,
                "missing_qty": missing_qty,
                "reason": "target_already_reached",
                "result": "skipped",
            },
        )
        return None
    if strategy._side_adds_remaining("short") <= 0:
        strategy.logger.info(
            "Phase 4 short rebuild skipped",
            extra={
                "event": "rebuild_skipped",
                "purpose": "phase4_short_rebuild",
                "side": "short",
                "state": state,
                "price": price,
                "short_size": short_size,
                "target_qty": target_short_qty,
                "missing_qty": missing_qty,
                "reason": "max_adds_reached",
                "result": "skipped",
            },
        )
        return None
    requested_qty = min(strategy._base_add_qty("short"), missing_qty)
    strategy.logger.debug(
        "Phase 4 short rebuild qty calculated",
        extra={
            "event": "phase4_short_rebuild_qty_calculated",
            "purpose": "phase4_short_rebuild",
            "side": "short",
            "state": state,
            "price": price,
            "short_size": short_size,
            "short_avg": short_avg,
            "target_qty": target_short_qty,
            "missing_qty": missing_qty,
            "raw_qty": requested_qty,
            "result": "calculating",
        },
    )
    normalized_qty = strategy._normalize_order_qty(requested_qty, "PHASE4_SHORT_REBUILD")
    if normalized_qty <= 0:
        return None
    capped_qty = min(normalized_qty, missing_qty)
    final_qty = strategy._normalize_order_qty(capped_qty, "PHASE4_SHORT_REBUILD")
    if final_qty <= 0:
        return None
    if not strategy._meets_min_order_value(price, final_qty, "PHASE4_SHORT_REBUILD"):
        return None
    if short_size > 0 and short_avg > 0:
        if not strategy._short_heal_improves_avg(short_size, short_avg, price, final_qty):
            strategy.logger.debug(
                "Phase 4 short rebuild skipped",
                extra={
                    "event": "rebuild_skipped",
                    "purpose": "phase4_short_rebuild",
                    "side": "short",
                    "state": state,
                    "price": price,
                    "short_size": short_size,
                    "short_avg": short_avg,
                    "target_qty": target_short_qty,
                    "missing_qty": missing_qty,
                    "normalized_qty": final_qty,
                    "reason": "avg_not_improved",
                    "result": "skipped",
                },
            )
            return None
    intent = OrderIntent(
        side="short",
        qty=final_qty,
        price=price,
        purpose="phase4_short_rebuild",
        order_type="Market",
    )
    strategy.logger.info(
        "Phase 4 short rebuild intent prepared",
        extra={
            "event": "order_intent_prepared",
            "purpose": intent.purpose,
            "side": intent.side,
            "state": state,
            "price": price,
            "short_size": short_size,
            "short_avg": short_avg,
            "target_qty": target_short_qty,
            "missing_qty": missing_qty,
            "raw_qty": requested_qty,
            "normalized_qty": final_qty,
            "reduce_only": False,
            "order_type": intent.order_type,
            "result": "prepared",
        },
    )
    return intent


def phase2_short_profit_budget_available(strategy: PSRHStrategy) -> float:
    realized_short_profit = max(0.0, strategy._realized_short_pnl_total)
    reserved_budget = max(0.0, strategy._phase2_short_profit_budget_reserved)
    return max(0.0, realized_short_profit - reserved_budget)


def maybe_build_phase2_long_reduce_intent(
    strategy: PSRHStrategy, price: float
) -> OrderIntent | None:
    if not strategy._phase2_long_reduce_ready():
        strategy.logger.debug(
            "Phase 2 long reduce skipped",
            extra={
                "event": "rebuild_skipped",
                "purpose": "phase2_long_reduce_from_short_profit",
                "side": "long",
                "state": strategy.state_machine.state.value,
                "price": price,
                "reason": "phase2_not_ready",
                "result": "skipped",
            },
        )
        return None
    return strategy._build_phase2_long_reduce_from_short_profit_intent(price)


def phase2_long_reduce_ready(strategy: PSRHStrategy) -> bool:
    _, short_size, _, _ = strategy._get_position_snapshot()
    return bool(
        strategy._phase2_short_profit_long_reduce_enabled()
        and strategy._aggressive_down_heal_phase_completed
        and short_size <= 1e-9
    )


def build_phase2_long_reduce_from_short_profit_intent(
    strategy: PSRHStrategy, price: float
) -> OrderIntent | None:
    long_size, _, long_avg, _ = strategy._get_position_snapshot()
    state = strategy.state_machine.state.value
    if long_size <= 0 or long_avg <= 0 or price <= 0:
        return None
    if price >= long_avg:
        return None
    available_budget = strategy._phase2_short_profit_budget_available()
    if available_budget <= 0:
        return None
    long_loss_per_unit = max(long_avg - price, 0.0)
    if long_loss_per_unit <= 0:
        return None
    max_budget_covered_qty = min(long_size, available_budget / long_loss_per_unit)
    strategy.logger.debug(
        "Phase 2 long reduce qty calculated",
        extra={
            "event": "phase2_long_reduce_from_short_profit_qty_calculated",
            "purpose": "phase2_long_reduce_from_short_profit",
            "side": "long",
            "state": state,
            "price": price,
            "long_size": long_size,
            "long_avg": long_avg,
            "raw_qty": max_budget_covered_qty,
            "basket_pnl": None,
            "reason": "short_profit_budget_available",
            "result": "calculating",
        },
    )
    intent = strategy._build_market_intent(
        "long",
        max_budget_covered_qty,
        price,
        "phase2_long_reduce_from_short_profit",
        reduce_only=True,
    )
    if not intent:
        strategy.logger.debug(
            "Phase 2 long reduce skipped",
            extra={
                "event": "rebuild_skipped",
                "purpose": "phase2_long_reduce_from_short_profit",
                "side": "long",
                "state": state,
                "price": price,
                "long_size": long_size,
                "long_avg": long_avg,
                "raw_qty": max_budget_covered_qty,
                "reason": "market_intent_not_built",
                "result": "skipped",
            },
        )
        return None
    reserved_budget = intent.qty * long_loss_per_unit
    intent.metadata = {
        "phase2_budget_candidate": True,
        "phase2_reserved_budget": reserved_budget,
        "phase2_planned_qty": intent.qty,
        "phase2_long_avg": long_avg,
    }
    strategy.logger.info(
        "Phase 2 long reduce built against realized short profit budget",
        extra={
            "event": "order_intent_prepared",
            "purpose": intent.purpose,
            "side": intent.side,
            "state": state,
            "price": price,
            "long_avg": long_avg,
            "long_loss_per_unit": long_loss_per_unit,
            "available_short_profit_budget": available_budget,
            "phase2_budget_candidate": reserved_budget,
            "remaining_short_profit_budget": strategy._phase2_short_profit_budget_available(),
            "phase2_qty": intent.qty,
            "raw_qty": max_budget_covered_qty,
            "normalized_qty": intent.qty,
            "reduce_only": intent.reduce_only,
            "order_type": intent.order_type,
            "result": "prepared",
        },
    )
    return intent


def record_phase2_short_profit_budget_usage(
    strategy: PSRHStrategy,
    client_order_id: str,
    order: dict[str, Any] | None,
    source: str,
) -> None:
    if strategy._normalize_purpose_name((order or {}).get("purpose")) != (
        "phase2_long_reduce_from_short_profit"
    ):
        return
    metadata = (order or {}).get("metadata") or {}
    planned_budget = float(metadata.get("phase2_reserved_budget") or 0.0)
    planned_qty = float(metadata.get("phase2_planned_qty") or 0.0)
    fill_qty = float(order.get("filled_qty") or order.get("qty") or order.get("size") or 0.0)
    budget_used = 0.0
    if planned_budget > 0 and planned_qty > 0 and fill_qty > 0:
        fill_ratio = min(max(fill_qty / planned_qty, 0.0), 1.0)
        budget_used = planned_budget * fill_ratio
    if budget_used <= 0:
        long_avg = float(metadata.get("phase2_long_avg") or 0.0)
        fill_price = float(metadata.get("last_fill_price") or order.get("price") or 0.0)
        if long_avg > 0 and fill_price > 0 and fill_qty > 0:
            budget_used = max(long_avg - fill_price, 0.0) * fill_qty
    if budget_used <= 0:
        return
    strategy._phase2_short_profit_budget_reserved += budget_used
    strategy.logger.info(
        "Phase 2 short-profit budget consumed on fill",
        extra={
            "client_order_id": client_order_id,
            "source": source,
            "budget_used": budget_used,
            "reserved_phase2_budget": strategy._phase2_short_profit_budget_reserved,
            "remaining_short_profit_budget": strategy._phase2_short_profit_budget_available(),
        },
    )


def ensure_aggressive_down_heal_tracking(
    strategy: PSRHStrategy, price: float
) -> None:
    if not strategy._aggressive_down_heal_enabled():
        return
    _, short_size, _, _ = strategy._get_position_snapshot()
    if short_size <= 0 or price <= 0:
        return
    if strategy._aggressive_down_heal_initial_short_size is None:
        strategy._aggressive_down_heal_initial_short_size = short_size
    if strategy._aggressive_down_heal_reference_price is None:
        strategy._aggressive_down_heal_reference_price = price


def aggressive_down_heal_complete(strategy: PSRHStrategy) -> bool:
    _, short_size, _, _ = strategy._get_position_snapshot()
    return (
        strategy._aggressive_down_heal_initial_short_size is not None
        and short_size <= 1e-9
    )


def confirmed_aggressive_down_heal_move(
    strategy: PSRHStrategy, price: float
) -> bool:
    strategy._ensure_aggressive_down_heal_tracking(price)
    reference_price = strategy._aggressive_down_heal_reference_price
    if not reference_price or price <= 0:
        return False
    step_pct = max(0.0, strategy.config.aggressive_down_heal_step_pct)
    if step_pct <= 0:
        return False
    return price <= reference_price * (1 - step_pct)


def build_aggressive_down_heal_short_intent(
    strategy: PSRHStrategy, price: float
) -> OrderIntent | None:
    long_size, short_size, _, _ = strategy._get_position_snapshot()
    state = strategy.state_machine.state.value
    if long_size <= 0 or short_size <= 0 or price <= 0:
        strategy.logger.debug(
            "Aggressive down-heal short skipped",
            extra={
                "event": "rebuild_skipped",
                "purpose": "aggressive_down_heal_short",
                "side": "short",
                "state": state,
                "price": price,
                "long_size": long_size,
                "short_size": short_size,
                "reason": "invalid_position_or_price",
                "result": "skipped",
            },
        )
        return None
    heal_qty = short_size * max(0.0, strategy.config.aggressive_down_heal_size_pct)
    heal_qty = min(heal_qty, short_size)
    strategy.logger.debug(
        "Aggressive down-heal short qty calculated",
        extra={
            "event": "aggressive_down_heal_short_qty_calculated",
            "purpose": "aggressive_down_heal_short",
            "side": "short",
            "state": state,
            "price": price,
            "long_size": long_size,
            "short_size": short_size,
            "raw_qty": heal_qty,
            "result": "calculated",
        },
    )
    if heal_qty <= 0:
        return None
    intent = strategy._build_market_intent(
        "short",
        heal_qty,
        price,
        "aggressive_down_heal_short",
        reduce_only=True,
    )
    if intent:
        strategy.logger.info(
            "Aggressive down-heal short intent prepared",
            extra={
                "event": "order_intent_prepared",
                "purpose": intent.purpose,
                "side": intent.side,
                "state": state,
                "price": price,
                "long_size": long_size,
                "short_size": short_size,
                "raw_qty": heal_qty,
                "normalized_qty": intent.qty,
                "reduce_only": intent.reduce_only,
                "order_type": intent.order_type,
                "result": "prepared",
            },
        )
    return intent

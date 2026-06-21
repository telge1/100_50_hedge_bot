from __future__ import annotations

from typing import TYPE_CHECKING

from strategy.execution.order_executor import OrderIntent

if TYPE_CHECKING:
    from emergency_100.final_hedge_strategy import PSRHStrategy


def build_spread_heal_long_intent(
    strategy: PSRHStrategy, price: float, spread_pct: float
) -> OrderIntent | None:
    long_size, _, long_avg, short_avg = strategy._get_position_snapshot()
    state = strategy.state_machine.state.value
    if long_size <= 0 or long_avg <= 0 or price <= 0:
        strategy.logger.debug(
            "Long heal skipped: zero long position or invalid price",
            extra={
                "event": "spread_heal_long_skipped",
                "purpose": "spread_heal_long",
                "side": "long",
                "state": state,
                "price": price,
                "long_size": long_size,
                "long_avg": long_avg,
                "short_avg": short_avg,
                "spread_pct": spread_pct,
                "reason": "invalid_position_or_price",
                "result": "skipped",
            },
        )
        return None
    if price >= long_avg:
        strategy.logger.debug(
            "Long heal skipped: price not below long_avg",
            extra={
                "event": "spread_heal_long_skipped",
                "purpose": "spread_heal_long",
                "side": "long",
                "state": state,
                "price": price,
                "long_size": long_size,
                "long_avg": long_avg,
                "short_avg": short_avg,
                "spread_pct": spread_pct,
                "reason": "price_not_below_long_avg",
                "result": "skipped",
            },
        )
        return None
    if short_avg > 0 and price >= short_avg * 1.01:
        strategy.logger.debug(
            "Long heal skipped: price high enough for short heal",
            extra={
                "event": "spread_heal_long_skipped",
                "purpose": "spread_heal_long",
                "side": "long",
                "state": state,
                "price": price,
                "long_size": long_size,
                "long_avg": long_avg,
                "short_avg": short_avg,
                "spread_pct": spread_pct,
                "reason": "price_high_enough_for_short_heal",
                "result": "skipped",
            },
        )
        return None
    if short_avg > 0 and price >= short_avg:
        strategy.logger.debug(
            "Long heal skipped: price above short_avg",
            extra={
                "event": "spread_heal_long_skipped",
                "purpose": "spread_heal_long",
                "side": "long",
                "state": state,
                "price": price,
                "long_size": long_size,
                "long_avg": long_avg,
                "short_avg": short_avg,
                "spread_pct": spread_pct,
                "reason": "price_above_short_avg",
                "result": "skipped",
            },
        )
        return None
    if spread_pct <= strategy.config.spread_heal_trigger_pct:
        strategy.logger.debug(
            "Long heal skipped: spread below healing threshold",
            extra={
                "event": "spread_heal_long_skipped",
                "purpose": "spread_heal_long",
                "side": "long",
                "state": state,
                "price": price,
                "long_size": long_size,
                "long_avg": long_avg,
                "short_avg": short_avg,
                "spread_pct": spread_pct,
                "reason": "spread_below_threshold",
                "result": "skipped",
            },
        )
        return None
    if strategy._long_heal_adds_remaining() <= 0:
        strategy.logger.info(
            "Long heal skipped: healed adds limit reached",
            extra={
                "event": "spread_heal_long_skipped",
                "purpose": "spread_heal_long",
                "side": "long",
                "state": state,
                "price": price,
                "long_size": long_size,
                "long_avg": long_avg,
                "short_avg": short_avg,
                "spread_pct": spread_pct,
                "long_heal_adds": strategy._long_heal_adds,
                "max_heal_adds": strategy.config.healing_max_adds_per_cycle,
                "reason": "max_adds_reached",
                "result": "skipped",
            },
        )
        return None
    add_qty = strategy._long_heal_add_qty()
    strategy.logger.debug(
        "Long heal qty calculated",
        extra={
            "event": "spread_heal_long_qty_calculated",
            "purpose": "spread_heal_long",
            "side": "long",
            "state": state,
            "price": price,
            "long_size": long_size,
            "long_avg": long_avg,
            "short_avg": short_avg,
            "spread_pct": spread_pct,
            "raw_qty": add_qty,
            "result": "calculated",
        },
    )
    if add_qty <= 0:
        strategy.logger.debug(
            "Long heal skipped: calculated add qty zero",
            extra={
                "event": "spread_heal_long_skipped",
                "purpose": "spread_heal_long",
                "side": "long",
                "state": state,
                "price": price,
                "long_size": long_size,
                "long_avg": long_avg,
                "short_avg": short_avg,
                "spread_pct": spread_pct,
                "raw_qty": add_qty,
                "reason": "raw_qty_zero",
                "result": "skipped",
            },
        )
        return None
    normalized_qty = strategy._normalize_order_qty(add_qty, "SPREAD_HEAL_LONG")
    if normalized_qty <= 0:
        strategy.logger.debug(
            "Long heal skipped: normalized qty zero",
            extra={
                "event": "spread_heal_long_skipped",
                "purpose": "spread_heal_long",
                "side": "long",
                "state": state,
                "price": price,
                "long_size": long_size,
                "long_avg": long_avg,
                "short_avg": short_avg,
                "spread_pct": spread_pct,
                "raw_qty": add_qty,
                "normalized_qty": normalized_qty,
                "reason": "normalized_qty_zero",
                "result": "skipped",
            },
        )
        return None
    if not strategy._meets_min_order_value(price, normalized_qty, "SPREAD_HEAL_LONG"):
        strategy.logger.debug(
            "Long heal skipped: below min order value",
            extra={
                "event": "spread_heal_long_skipped",
                "purpose": "spread_heal_long",
                "side": "long",
                "state": state,
                "price": price,
                "long_size": long_size,
                "long_avg": long_avg,
                "short_avg": short_avg,
                "spread_pct": spread_pct,
                "raw_qty": add_qty,
                "normalized_qty": normalized_qty,
                "reason": "below_min_order_value",
                "result": "skipped",
            },
        )
        return None
    if not strategy._long_heal_improves_avg(long_size, long_avg, price, normalized_qty):
        strategy.logger.debug(
            "Long heal skipped: avg would not improve",
            extra={
                "event": "spread_heal_long_skipped",
                "purpose": "spread_heal_long",
                "side": "long",
                "state": state,
                "price": price,
                "long_size": long_size,
                "long_avg": long_avg,
                "short_avg": short_avg,
                "spread_pct": spread_pct,
                "normalized_qty": normalized_qty,
                "reason": "avg_not_improved",
                "result": "skipped",
            },
        )
        return None
    intent = OrderIntent(
        side="long",
        qty=normalized_qty,
        price=price,
        purpose="spread_heal_long",
        order_type="Market",
    )
    strategy.logger.info(
        "Long heal intent prepared",
        extra={
            "event": "order_intent_prepared",
            "purpose": intent.purpose,
            "side": intent.side,
            "state": state,
            "price": price,
            "long_size": long_size,
            "long_avg": long_avg,
            "short_avg": short_avg,
            "spread_pct": spread_pct,
            "raw_qty": add_qty,
            "normalized_qty": normalized_qty,
            "reduce_only": False,
            "order_type": intent.order_type,
            "result": "prepared",
        },
    )
    return intent


def build_spread_heal_short_intent(
    strategy: PSRHStrategy, price: float, spread_pct: float
) -> OrderIntent | None:
    _, short_size, _, short_avg = strategy._get_position_snapshot()
    state = strategy.state_machine.state.value
    if short_size <= 0 or short_avg <= 0 or price <= 0:
        strategy.logger.debug(
            "Short heal skipped: zero short position or invalid price",
            extra={
                "event": "spread_heal_short_skipped",
                "purpose": "spread_heal_short",
                "side": "short",
                "state": state,
                "price": price,
                "short_size": short_size,
                "short_avg": short_avg,
                "spread_pct": spread_pct,
                "reason": "invalid_position_or_price",
                "result": "skipped",
            },
        )
        return None
    if spread_pct <= strategy.config.spread_heal_trigger_pct:
        strategy.logger.debug(
            "Short heal skipped: spread below healing threshold",
            extra={
                "event": "spread_heal_short_skipped",
                "purpose": "spread_heal_short",
                "side": "short",
                "state": state,
                "price": price,
                "short_size": short_size,
                "short_avg": short_avg,
                "spread_pct": spread_pct,
                "reason": "spread_below_threshold",
                "result": "skipped",
            },
        )
        return None
    if price < short_avg * 1.01:
        strategy.logger.debug(
            "Short heal skipped: price below short_avg * 1.01",
            extra={
                "event": "spread_heal_short_skipped",
                "purpose": "spread_heal_short",
                "side": "short",
                "state": state,
                "price": price,
                "short_size": short_size,
                "short_avg": short_avg,
                "spread_pct": spread_pct,
                "reason": "price_below_short_avg_buffer",
                "result": "skipped",
            },
        )
        return None
    if strategy._short_heal_adds_remaining() <= 0:
        strategy.logger.info(
            "Short heal skipped: healed adds limit reached",
            extra={
                "event": "spread_heal_short_skipped",
                "purpose": "spread_heal_short",
                "side": "short",
                "state": state,
                "price": price,
                "short_size": short_size,
                "short_avg": short_avg,
                "spread_pct": spread_pct,
                "short_heal_adds": strategy._short_heal_adds,
                "max_heal_adds": strategy.config.healing_max_adds_per_cycle,
                "reason": "max_adds_reached",
                "result": "skipped",
            },
        )
        return None
    add_qty = strategy._short_heal_add_qty()
    strategy.logger.debug(
        "Short heal qty calculated",
        extra={
            "event": "spread_heal_short_qty_calculated",
            "purpose": "spread_heal_short",
            "side": "short",
            "state": state,
            "price": price,
            "short_size": short_size,
            "short_avg": short_avg,
            "spread_pct": spread_pct,
            "raw_qty": add_qty,
            "result": "calculated",
        },
    )
    if add_qty <= 0:
        strategy.logger.debug(
            "Short heal skipped: calculated add qty zero",
            extra={
                "event": "spread_heal_short_skipped",
                "purpose": "spread_heal_short",
                "side": "short",
                "state": state,
                "price": price,
                "short_size": short_size,
                "short_avg": short_avg,
                "spread_pct": spread_pct,
                "raw_qty": add_qty,
                "reason": "raw_qty_zero",
                "result": "skipped",
            },
        )
        return None
    normalized_qty = strategy._normalize_order_qty(add_qty, "SPREAD_HEAL_SHORT")
    if normalized_qty <= 0:
        strategy.logger.debug(
            "Short heal skipped: normalized qty zero",
            extra={
                "event": "spread_heal_short_skipped",
                "purpose": "spread_heal_short",
                "side": "short",
                "state": state,
                "price": price,
                "short_size": short_size,
                "short_avg": short_avg,
                "spread_pct": spread_pct,
                "raw_qty": add_qty,
                "normalized_qty": normalized_qty,
                "reason": "normalized_qty_zero",
                "result": "skipped",
            },
        )
        return None
    if not strategy._meets_min_order_value(price, normalized_qty, "SPREAD_HEAL_SHORT"):
        strategy.logger.debug(
            "Short heal skipped: below min order value",
            extra={
                "event": "spread_heal_short_skipped",
                "purpose": "spread_heal_short",
                "side": "short",
                "state": state,
                "price": price,
                "short_size": short_size,
                "short_avg": short_avg,
                "spread_pct": spread_pct,
                "raw_qty": add_qty,
                "normalized_qty": normalized_qty,
                "reason": "below_min_order_value",
                "result": "skipped",
            },
        )
        return None
    intent = OrderIntent(
        side="short",
        qty=normalized_qty,
        price=price,
        purpose="spread_heal_short",
        order_type="Market",
        reduce_only=True,
    )
    strategy.logger.info(
        "Short heal intent prepared",
        extra={
            "event": "order_intent_prepared",
            "purpose": intent.purpose,
            "side": intent.side,
            "state": state,
            "price": price,
            "short_size": short_size,
            "short_avg": short_avg,
            "spread_pct": spread_pct,
            "raw_qty": add_qty,
            "normalized_qty": normalized_qty,
            "reduce_only": True,
            "order_type": intent.order_type,
            "result": "prepared",
        },
    )
    return intent

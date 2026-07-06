from __future__ import annotations

import math
from decimal import Decimal, ROUND_DOWN
from typing import Any

from fixed_cycle_hedge_bot.models import FillEvent, StrategyIntent

from ..simulated_execution import fill_order_at_candle_close
from ..simulated_pnl import closed_pnl_for_virtual_order_fill
from .calculations import (
    compute_net_long_qty,
    compute_price_drop_pct,
    is_exactly_neutral,
    would_exceed_loss_budget,
)
from .config import RecoveryBotConfig
from .state import RecoveryBotTracker, RecoveryState

RECOVERY_NEUTRALIZE_LONG_PURPOSE = "RECOVERY_NEUTRALIZE_LONG"
RECOVERY_PAIR_REDUCE_LONG_PURPOSE = "RECOVERY_PAIR_REDUCE_LONG"
RECOVERY_PAIR_REDUCE_SHORT_PURPOSE = "RECOVERY_PAIR_REDUCE_SHORT"
RECOVERY_FROZEN_STATES = frozenset(
    {
        RecoveryState.NEUTRALIZING,
        RecoveryState.PAIR_REDUCING,
        RecoveryState.MINIMUM_PAIR_REACHED,
        RecoveryState.READY_TO_CLOSE,
        RecoveryState.WAITING_FOR_RELOAD,
    }
)


def validate_recovery_mode_exclusivity(
    *,
    recovery_bot_config: RecoveryBotConfig | None,
    stuck_recovery_reload_config: Any | None,
) -> None:
    """Reject incompatible backtest-only recovery features."""
    recovery_enabled = bool(recovery_bot_config is not None and recovery_bot_config.enabled)
    reload_enabled = bool(
        stuck_recovery_reload_config is not None
        and bool(getattr(stuck_recovery_reload_config, "enabled", False))
    )
    if recovery_enabled and reload_enabled:
        raise ValueError(
            "recovery_bot and stuck_recovery_reload cannot be enabled together"
        )


def is_recovery_strategy_frozen(tracker: RecoveryBotTracker | None) -> bool:
    if tracker is None:
        return False
    return tracker.state in RECOVERY_FROZEN_STATES


def _rule_decimal(rules: dict[str, Decimal], key: str, default: str) -> Decimal:
    value = rules.get(key)
    if value is None:
        return Decimal(default)
    return Decimal(str(value))


def _instrument_rules(sim) -> dict[str, Decimal]:
    return sim.runtime_state.instrument_rules.get(sim.symbol.upper(), {})


def _qty_step_tolerance(sim) -> float:
    rules = _instrument_rules(sim)
    step = _rule_decimal(rules, "qty_step", "0.001")
    return max(float(step), 1e-12)


def _round_down_to_step(value: float, step: float) -> float:
    if value <= 0 or step <= 0:
        return 0.0
    dec_value = Decimal(str(value))
    dec_step = Decimal(str(step))
    units = (dec_value / dec_step).to_integral_value(rounding=ROUND_DOWN)
    return float(units * dec_step)


def _resolve_fee_rate(sim, config: RecoveryBotConfig) -> float | None:
    if not bool(config.include_fees):
        return None
    if sim.book.fee_rate is not None:
        return max(float(sim.book.fee_rate), 0.0)
    strategy_rate_pct = float(getattr(sim.config, "order_fee_rate_pct", 0.0) or 0.0)
    if strategy_rate_pct <= 0:
        return None
    return strategy_rate_pct / 100.0


def _estimate_recovery_loss_usdt(
    sim,
    tracker: RecoveryBotTracker,
    *,
    qty: float,
    current_price: float,
) -> float:
    config = tracker.config
    if qty <= 0:
        return 0.0
    slippage_pct = max(float(config.slippage_buffer_pct or 0.0), 0.0) / 100.0
    conservative_fill_price = float(current_price) * max(0.0, 1.0 - slippage_pct)
    fee_rate = _resolve_fee_rate(sim, config)
    pnl, _details = closed_pnl_for_virtual_order_fill(
        side="long",
        reduce_only=True,
        avg_entry_price=float(sim.book.long_avg or 0.0),
        fill_price=float(conservative_fill_price),
        qty=float(qty),
        fee_rate=fee_rate,
    )
    if bool(config.include_funding):
        # No funding model exists in the simulator yet. Phase 3 keeps funding
        # neutral rather than inventing a second accounting path.
        pass
    return max(0.0, -float(pnl))


def _slippage_buffer_fraction(config: RecoveryBotConfig) -> float:
    return max(float(config.slippage_buffer_pct or 0.0), 0.0) / 100.0


def _conservative_pair_prices(
    current_price: float,
    config: RecoveryBotConfig,
) -> tuple[float, float]:
    slippage = _slippage_buffer_fraction(config)
    long_fill_price = float(current_price) * max(0.0, 1.0 - slippage)
    short_fill_price = float(current_price) * (1.0 + slippage)
    return float(long_fill_price), float(short_fill_price)


def _recovery_metadata(sim, tracker: RecoveryBotTracker) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "source": "recovery_bot",
        "recovery_state": str(tracker.state),
        "recovery_runs_for_trade": int(tracker.recovery_runs_for_trade),
        "blocked_reason": tracker.blocked_reason,
        "loss_budget_usdt": tracker.loss_budget_usdt,
        "loss_budget_used_usdt": tracker.loss_budget_used_usdt,
    }
    fee_rate = _resolve_fee_rate(sim, tracker.config)
    if fee_rate is not None:
        metadata["fee_rate"] = fee_rate
    return metadata


def _cancel_conflicting_active_orders(sim) -> int:
    """Cancel all non-recovery active orders and log the cancellations."""
    cancelled = 0
    for order in list(sim.book.active_orders()):
        purpose = str(getattr(order, "purpose", "") or "")
        if purpose.startswith("RECOVERY_"):
            continue
        if not sim.book.cancel_by_order_id(order.order_id):
            continue
        cancelled += 1
        sim._record_order_event(
            order,
            event_type="cancelled",
            status="CANCELED",
        )
    if cancelled > 0:
        sim.book.sync_runtime_state(sim.runtime_state)
        sim._refresh_snapshot_from_book(
            source="after_recovery_cancel_conflicts",
            price=sim.candle.close,
        )
    return cancelled


def ensure_recovery_exclusive_order_state(sim, tracker: RecoveryBotTracker | None) -> int:
    """Cancel conflicting orders once when recovery starts controlling the trade."""
    if tracker is None:
        return 0
    if not is_recovery_strategy_frozen(tracker):
        return 0
    if bool(tracker.extra.get("exclusive_order_state_established")):
        return 0
    cancelled = _cancel_conflicting_active_orders(sim)
    tracker.extra["exclusive_order_state_established"] = True
    return cancelled


def _compute_neutralization_reduce_qty(sim, tracker: RecoveryBotTracker) -> float:
    config = tracker.config
    current_long_qty = float(sim.book.long_qty or 0.0)
    current_short_qty = float(sim.book.short_qty or 0.0)
    current_net_long = max(compute_net_long_qty(current_long_qty, current_short_qty), 0.0)
    if current_net_long <= 0:
        return 0.0

    mode = str(config.neutralize_reduce_mode or "fixed_steps").strip()
    if mode == "fixed_steps":
        raw_qty = float(tracker.neutralization_fixed_step_qty or 0.0)
    elif mode == "fixed_qty":
        raw_qty = float(config.neutralize_reduce_qty or 0.0)
    elif mode == "percent":
        raw_qty = current_net_long * float(config.neutralize_reduce_pct or 0.0) / 100.0
    else:
        raw_qty = 0.0

    desired_qty = min(max(raw_qty, 0.0), current_net_long)
    rules = _instrument_rules(sim)
    qty_step = float(_rule_decimal(rules, "qty_step", "0.001"))
    min_order_qty = float(_rule_decimal(rules, "min_order_qty", "0.001"))
    min_notional = float(_rule_decimal(rules, "min_notional", "5"))
    rounded_qty = _round_down_to_step(desired_qty, qty_step)
    if rounded_qty <= 0:
        return 0.0
    if rounded_qty > current_net_long:
        rounded_qty = _round_down_to_step(current_net_long, qty_step)
    if rounded_qty <= 0:
        return 0.0
    if rounded_qty < min_order_qty:
        return 0.0
    if float(sim.candle.close) * rounded_qty < min_notional:
        return 0.0
    # Hard guard: never over-neutralize.
    return min(float(rounded_qty), float(current_net_long))


def _current_pair_qty(sim) -> float:
    return min(float(sim.book.long_qty or 0.0), float(sim.book.short_qty or 0.0))


def _minimum_pair_reached(sim, tracker: RecoveryBotTracker, *, current_price: float) -> bool:
    config = tracker.config
    pair_qty = _current_pair_qty(sim)
    if pair_qty <= 0:
        return True
    tolerance = _qty_step_tolerance(sim)
    if pair_qty <= float(config.minimum_pair_qty or 0.0) + tolerance:
        return True
    conservative_price = float(current_price) * max(0.0, 1.0 - _slippage_buffer_fraction(config))
    if conservative_price <= 0:
        return False
    if pair_qty * conservative_price <= float(config.minimum_pair_notional_usdt or 0.0) + 1e-12:
        return True
    return False


def _estimate_pair_combined_loss_usdt(
    sim,
    tracker: RecoveryBotTracker,
    *,
    qty: float,
    current_price: float,
) -> tuple[float, float]:
    config = tracker.config
    if qty <= 0:
        return 0.0, 0.0
    fee_rate = _resolve_fee_rate(sim, config)
    long_fill_price, short_fill_price = _conservative_pair_prices(float(current_price), config)
    long_pnl, _ = closed_pnl_for_virtual_order_fill(
        side="long",
        reduce_only=True,
        avg_entry_price=float(sim.book.long_avg or 0.0),
        fill_price=float(long_fill_price),
        qty=float(qty),
        fee_rate=fee_rate,
    )
    short_pnl, _ = closed_pnl_for_virtual_order_fill(
        side="short",
        reduce_only=True,
        avg_entry_price=float(sim.book.short_avg or 0.0),
        fill_price=float(short_fill_price),
        qty=float(qty),
        fee_rate=fee_rate,
    )
    combined = float(long_pnl) + float(short_pnl)
    return max(0.0, -combined), combined


def _compute_pair_reduce_qty(sim, tracker: RecoveryBotTracker, *, current_price: float) -> float:
    config = tracker.config
    pair_qty = _current_pair_qty(sim)
    if pair_qty <= 0:
        return 0.0

    mode = str(config.pair_reduce_mode or "fixed_qty").strip()
    if mode == "fixed_qty":
        raw_qty = float(config.pair_reduce_qty or 0.0)
    elif mode == "percent":
        raw_qty = pair_qty * float(config.pair_reduce_pct or 0.0) / 100.0
    else:
        raw_qty = 0.0
    raw_qty = max(raw_qty, 0.0)

    conservative_price = float(current_price) * max(0.0, 1.0 - _slippage_buffer_fraction(config))
    max_reduce_by_qty = max(0.0, pair_qty - float(config.minimum_pair_qty or 0.0))
    if conservative_price > 0 and float(config.minimum_pair_notional_usdt or 0.0) > 0:
        min_pair_by_notional = float(config.minimum_pair_notional_usdt or 0.0) / conservative_price
        max_reduce_by_notional = max(0.0, pair_qty - min_pair_by_notional)
    else:
        max_reduce_by_notional = pair_qty
    max_allowed = min(pair_qty, max_reduce_by_qty, max_reduce_by_notional)
    desired_qty = min(raw_qty, max_allowed)

    rules = _instrument_rules(sim)
    qty_step = float(_rule_decimal(rules, "qty_step", "0.001"))
    min_order_qty = float(_rule_decimal(rules, "min_order_qty", "0.001"))
    min_notional = float(_rule_decimal(rules, "min_notional", "5"))
    rounded_qty = _round_down_to_step(desired_qty, qty_step)
    if rounded_qty <= 0:
        return 0.0
    if rounded_qty > max_allowed:
        rounded_qty = _round_down_to_step(max_allowed, qty_step)
    if rounded_qty <= 0:
        return 0.0
    if rounded_qty < min_order_qty:
        return 0.0
    if conservative_price * rounded_qty < min_notional:
        return 0.0
    return min(float(rounded_qty), float(pair_qty))


def _maybe_mark_pair_reducing(
    sim,
    tracker: RecoveryBotTracker,
    *,
    anchor_price: float,
) -> bool:
    tolerance = _qty_step_tolerance(sim)
    if not is_exactly_neutral(
        float(sim.book.long_qty or 0.0),
        float(sim.book.short_qty or 0.0),
        tolerance_qty=tolerance,
    ):
        return False
    tracker.state = RecoveryState.PAIR_REDUCING
    tracker.pair_anchor_price = float(anchor_price)
    tracker.remaining_long_qty = float(sim.book.long_qty or 0.0)
    tracker.remaining_short_qty = float(sim.book.short_qty or 0.0)
    tracker.blocked_reason = None
    return True


def _evaluate_post_minimum_state(
    sim,
    tracker: RecoveryBotTracker,
    *,
    current_price: float,
) -> bool:
    if tracker.state != RecoveryState.MINIMUM_PAIR_REACHED:
        return False
    pair_qty = _current_pair_qty(sim)
    additional_loss, combined_pnl = _estimate_pair_combined_loss_usdt(
        sim,
        tracker,
        qty=float(pair_qty),
        current_price=float(current_price),
    )
    tracker.final_exit_reason = "full_exit_within_budget" if not would_exceed_loss_budget(
        tracker.loss_budget_usdt,
        tracker.loss_budget_used_usdt,
        additional_loss,
    ) else "full_exit_outside_budget"
    if would_exceed_loss_budget(
        tracker.loss_budget_usdt,
        tracker.loss_budget_used_usdt,
        additional_loss,
    ):
        tracker.state = RecoveryState.WAITING_FOR_RELOAD
    else:
        tracker.state = RecoveryState.READY_TO_CLOSE
    return True


def maybe_execute_neutralization_step(
    sim,
    tracker: RecoveryBotTracker,
    *,
    current_price: float,
    candle_index: int,
) -> list[FillEvent]:
    """Execute at most one neutralization step via a real simulated market fill."""
    if tracker is None or tracker.state != RecoveryState.NEUTRALIZING:
        return []

    # Ensure conflicting orders are cancelled once before the first recovery-
    # controlled candle/step. Afterwards the frozen strategy path prevents new
    # normal orders from being created each candle.
    ensure_recovery_exclusive_order_state(sim, tracker)

    current_long_qty = float(sim.book.long_qty or 0.0)
    current_short_qty = float(sim.book.short_qty or 0.0)
    current_net_long = max(compute_net_long_qty(current_long_qty, current_short_qty), 0.0)
    tracker.remaining_long_qty = current_long_qty
    tracker.remaining_short_qty = current_short_qty
    tracker.last_action_candle_index = int(candle_index)

    if current_net_long <= 0:
        _maybe_mark_pair_reducing(sim, tracker, anchor_price=float(current_price))
        return []

    anchor_price = float(tracker.neutralization_anchor_price or tracker.recovery_start_price or current_price)
    if compute_price_drop_pct(float(current_price), anchor_price) < float(
        tracker.config.neutralize_step_price_drop_pct or 0.0
    ):
        tracker.blocked_reason = None
        return []

    reduce_qty = _compute_neutralization_reduce_qty(sim, tracker)
    tolerance = _qty_step_tolerance(sim)
    if reduce_qty <= 0:
        if current_net_long <= tolerance:
            _maybe_mark_pair_reducing(sim, tracker, anchor_price=float(current_price))
        else:
            tracker.blocked_reason = "neutralization_untradeable_residual"
        return []

    expected_loss = _estimate_recovery_loss_usdt(
        sim,
        tracker,
        qty=float(reduce_qty),
        current_price=float(current_price),
    )
    if would_exceed_loss_budget(
        tracker.loss_budget_usdt,
        tracker.loss_budget_used_usdt,
        expected_loss,
    ):
        tracker.blocked_reason = "neutralization_blocked_by_loss_budget"
        return []

    intent = StrategyIntent(
        side="long",
        qty=float(reduce_qty),
        purpose=RECOVERY_NEUTRALIZE_LONG_PURPOSE,
        order_type="Market",
        reduce_only=True,
        position_idx=1,
        metadata=_recovery_metadata(sim, tracker),
    )
    intent_idx = sim._log_intent(intent, event_source="recovery_neutralization")
    order = sim._submit_intent_with_logging(
        intent,
        replace=False,
        intent_log_index=intent_idx,
    )
    if order is None:
        tracker.blocked_reason = "neutralization_order_submit_failed"
        return []

    sim.orders_submitted += 1
    fill_event = fill_order_at_candle_close(
        book=sim.book,
        runtime_state=sim.runtime_state,
        order_id=order.order_id,
        candle=sim.candle,
    )
    filled_order = sim.book.get_order(fill_event.client_order_id)
    if filled_order is not None:
        sim._record_order_event(
            filled_order,
            event_type="filled",
            status="FILLED",
        )
        fill_price = float(fill_event.exec_price)
        long_qty_after = float(sim.book.long_qty or 0.0)
        short_qty_after = float(sim.book.short_qty or 0.0)
        long_avg_after = float(sim.book.long_avg or 0.0)
        short_avg_after = float(sim.book.short_avg or 0.0)
        for row in reversed(sim.order_log):
            if row.get("order_id") != filled_order.order_id:
                continue
            if str(row.get("event_type") or "") != "filled":
                continue
            if row.get("price") in (None, ""):
                row["price"] = fill_price
            row.setdefault("fill_price", fill_price)
            row.setdefault("long_qty_after", long_qty_after)
            row.setdefault("short_qty_after", short_qty_after)
            row.setdefault("long_avg_after", long_avg_after)
            row.setdefault("short_avg_after", short_avg_after)
            break

    sim.book.sync_runtime_state(sim.runtime_state)
    sim._refresh_snapshot_from_book(
        source="after_recovery_neutralization_fill",
        price=sim.candle.close,
    )

    tracker.neutralization_steps_done += 1
    tracker.last_action_candle_index = int(candle_index)
    tracker.remaining_long_qty = float(sim.book.long_qty or 0.0)
    tracker.remaining_short_qty = float(sim.book.short_qty or 0.0)
    tracker.neutralization_anchor_price = float(fill_event.exec_price)
    tracker.blocked_reason = None

    closed_pnl = float((fill_event.metadata or {}).get("closed_pnl") or 0.0)
    if closed_pnl < 0:
        tracker.loss_budget_used_usdt += abs(closed_pnl)
    tracker.recovery_realized_pnl += closed_pnl

    _maybe_mark_pair_reducing(
        sim,
        tracker,
        anchor_price=float(fill_event.exec_price),
    )
    return [fill_event]


def maybe_execute_pair_reduction_step(
    sim,
    tracker: RecoveryBotTracker,
    *,
    current_price: float,
    candle_index: int,
) -> list[FillEvent]:
    if tracker is None or tracker.state != RecoveryState.PAIR_REDUCING:
        return []

    ensure_recovery_exclusive_order_state(sim, tracker)
    tolerance = _qty_step_tolerance(sim)
    long_qty = float(sim.book.long_qty or 0.0)
    short_qty = float(sim.book.short_qty or 0.0)
    if not is_exactly_neutral(long_qty, short_qty, tolerance_qty=tolerance):
        tracker.blocked_reason = "pair_not_neutral"
        return []

    if _minimum_pair_reached(sim, tracker, current_price=float(current_price)):
        tracker.state = RecoveryState.MINIMUM_PAIR_REACHED
        tracker.minimum_pair_reached = True
        tracker.remaining_long_qty = long_qty
        tracker.remaining_short_qty = short_qty
        tracker.last_action_candle_index = int(candle_index)
        tracker.blocked_reason = None
        return []

    anchor_price = float(tracker.pair_anchor_price or current_price)
    move_pct = float(tracker.config.pair_reduce_move_pct or 0.0)
    up_triggered = bool(tracker.config.pair_reduce_on_up_move) and float(current_price) >= anchor_price * (1.0 + move_pct / 100.0)
    down_triggered = bool(tracker.config.pair_reduce_on_down_move) and float(current_price) <= anchor_price * (1.0 - move_pct / 100.0)
    if not (up_triggered or down_triggered):
        tracker.blocked_reason = None
        return []

    reduce_qty = _compute_pair_reduce_qty(sim, tracker, current_price=float(current_price))
    if reduce_qty <= 0:
        if _minimum_pair_reached(sim, tracker, current_price=float(current_price)):
            tracker.state = RecoveryState.MINIMUM_PAIR_REACHED
            tracker.minimum_pair_reached = True
            tracker.remaining_long_qty = float(sim.book.long_qty or 0.0)
            tracker.remaining_short_qty = float(sim.book.short_qty or 0.0)
            tracker.last_action_candle_index = int(candle_index)
            tracker.blocked_reason = None
        else:
            tracker.blocked_reason = "pair_reduction_untradeable"
        return []

    expected_loss, _combined_estimate = _estimate_pair_combined_loss_usdt(
        sim,
        tracker,
        qty=float(reduce_qty),
        current_price=float(current_price),
    )
    if would_exceed_loss_budget(
        tracker.loss_budget_usdt,
        tracker.loss_budget_used_usdt,
        expected_loss,
    ):
        tracker.blocked_reason = "pair_reduction_blocked_by_loss_budget"
        return []

    metadata = _recovery_metadata(sim, tracker)
    long_intent = StrategyIntent(
        side="long",
        qty=float(reduce_qty),
        purpose=RECOVERY_PAIR_REDUCE_LONG_PURPOSE,
        order_type="Market",
        reduce_only=True,
        position_idx=1,
        metadata=dict(metadata),
    )
    short_intent = StrategyIntent(
        side="short",
        qty=float(reduce_qty),
        purpose=RECOVERY_PAIR_REDUCE_SHORT_PURPOSE,
        order_type="Market",
        reduce_only=True,
        position_idx=2,
        metadata=dict(metadata),
    )

    long_idx = sim._log_intent(long_intent, event_source="recovery_pair_reduction")
    short_idx = sim._log_intent(short_intent, event_source="recovery_pair_reduction")
    long_order = sim._submit_intent_with_logging(long_intent, replace=False, intent_log_index=long_idx)
    short_order = sim._submit_intent_with_logging(short_intent, replace=False, intent_log_index=short_idx)
    if long_order is None or short_order is None:
        tracker.state = RecoveryState.FAILED
        tracker.blocked_reason = "pair_reduction_atomicity_failed"
        return []

    sim.orders_submitted += 2
    fill_events: list[FillEvent] = []
    try:
        fill_events.append(
            fill_order_at_candle_close(
                book=sim.book,
                runtime_state=sim.runtime_state,
                order_id=long_order.order_id,
                candle=sim.candle,
            )
        )
        fill_events.append(
            fill_order_at_candle_close(
                book=sim.book,
                runtime_state=sim.runtime_state,
                order_id=short_order.order_id,
                candle=sim.candle,
            )
        )
    except Exception:
        tracker.state = RecoveryState.FAILED
        tracker.blocked_reason = "pair_reduction_atomicity_failed"
        return fill_events

    for fill_event in fill_events:
        filled_order = sim.book.get_order(fill_event.client_order_id)
        if filled_order is not None:
            sim._record_order_event(
                filled_order,
                event_type="filled",
                status="FILLED",
            )
            fill_price = float(fill_event.exec_price)
            long_qty_after = float(sim.book.long_qty or 0.0)
            short_qty_after = float(sim.book.short_qty or 0.0)
            long_avg_after = float(sim.book.long_avg or 0.0)
            short_avg_after = float(sim.book.short_avg or 0.0)
            for row in reversed(sim.order_log):
                if row.get("order_id") != filled_order.order_id:
                    continue
                if str(row.get("event_type") or "") != "filled":
                    continue
                if row.get("price") in (None, ""):
                    row["price"] = fill_price
                row.setdefault("fill_price", fill_price)
                row.setdefault("long_qty_after", long_qty_after)
                row.setdefault("short_qty_after", short_qty_after)
                row.setdefault("long_avg_after", long_avg_after)
                row.setdefault("short_avg_after", short_avg_after)
                break

    sim.book.sync_runtime_state(sim.runtime_state)
    sim._refresh_snapshot_from_book(
        source="after_recovery_pair_reduction_fill",
        price=sim.candle.close,
    )

    combined_closed_pnl = sum(float((fill.metadata or {}).get("closed_pnl") or 0.0) for fill in fill_events)
    if combined_closed_pnl < 0:
        tracker.loss_budget_used_usdt += abs(combined_closed_pnl)
    tracker.pair_reduction_realized_pnl += combined_closed_pnl
    tracker.recovery_realized_pnl += combined_closed_pnl
    tracker.pair_reduction_steps_done += 1
    tracker.last_action_candle_index = int(candle_index)
    tracker.remaining_long_qty = float(sim.book.long_qty or 0.0)
    tracker.remaining_short_qty = float(sim.book.short_qty or 0.0)
    tracker.pair_anchor_price = float(sim.candle.close)
    tracker.blocked_reason = None

    if _minimum_pair_reached(sim, tracker, current_price=float(current_price)):
        tracker.state = RecoveryState.MINIMUM_PAIR_REACHED
        tracker.minimum_pair_reached = True

    return fill_events


def maybe_advance_minimum_pair_state(
    sim,
    tracker: RecoveryBotTracker,
    *,
    current_price: float,
) -> bool:
    return _evaluate_post_minimum_state(sim, tracker, current_price=float(current_price))


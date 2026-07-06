from __future__ import annotations

import math
from decimal import Decimal, ROUND_DOWN
from typing import Any

from fixed_cycle_hedge_bot.models import FillEvent, StrategyIntent

from ..simulated_execution import fill_order_at_candle_close, fill_order_at_price
from ..simulated_pnl import closed_pnl_for_virtual_order_fill
from .calculations import (
    compute_net_long_qty,
    compute_price_drop_pct,
    is_exactly_neutral,
    would_exceed_loss_budget,
)
from .config import RecoveryBotConfig
from .state import (
    RecoveryBotTracker,
    RecoveryState,
    append_recovery_trace,
)

RECOVERY_NEUTRALIZE_LONG_PURPOSE = "RECOVERY_NEUTRALIZE_LONG"
RECOVERY_PAIR_REDUCE_LONG_PURPOSE = "RECOVERY_PAIR_REDUCE_LONG"
RECOVERY_PAIR_REDUCE_SHORT_PURPOSE = "RECOVERY_PAIR_REDUCE_SHORT"
RECOVERY_FINAL_EXIT_LONG_PURPOSE = "RECOVERY_FINAL_EXIT_LONG"
RECOVERY_FINAL_EXIT_SHORT_PURPOSE = "RECOVERY_FINAL_EXIT_SHORT"
RECOVERY_RELOAD_LONG_PURPOSE = "RECOVERY_RELOAD_LONG"
RECOVERY_RELOAD_SHORT_PURPOSE = "RECOVERY_RELOAD_SHORT"
RECOVERY_FROZEN_STATES = frozenset(
    {
        RecoveryState.NEUTRALIZING,
        RecoveryState.PAIR_REDUCING,
        RecoveryState.MINIMUM_PAIR_REACHED,
        RecoveryState.READY_TO_CLOSE,
        RecoveryState.WAITING_FOR_RELOAD,
        RecoveryState.FAILED,
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
    step_float = float(step)
    return max(step_float + _step_alignment_tolerance(step_float), 1e-12)


def _step_alignment_tolerance(step: float) -> float:
    return max(abs(float(step)) * 1e-8, 1e-12)


def _is_step_aligned_qty(qty: float, step: float) -> bool:
    if step <= 0:
        return False
    units = float(qty) / float(step)
    nearest_units = round(units)
    return math.isclose(
        float(units),
        float(nearest_units),
        rel_tol=0.0,
        abs_tol=_step_alignment_tolerance(step),
    )


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


def _reload_fill_prices(
    current_price: float,
    config: RecoveryBotConfig,
) -> tuple[float, float]:
    slippage = max(float(config.reload_slippage_pct or 0.0), 0.0) / 100.0
    long_fill_price = float(current_price) * (1.0 + slippage)
    short_fill_price = float(current_price) * max(0.0, 1.0 - slippage)
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


def _mark_waiting_for_reload(
    tracker: RecoveryBotTracker,
    *,
    candle_index: int | None,
    reason: str,
) -> None:
    tracker.state = RecoveryState.WAITING_FOR_RELOAD
    tracker.final_exit_reason = reason
    if candle_index is not None:
        tracker.waiting_for_reload_since_candle_index = int(candle_index)
        tracker.last_action_candle_index = int(candle_index)
    tracker.reload_attempted = False
    tracker.reload_candle_index = None
    tracker.reload_long_qty = 0.0
    tracker.reload_short_qty = 0.0
    tracker.reload_reason = None


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


def _cancel_all_active_orders(sim) -> int:
    cancelled = 0
    for order in list(sim.book.active_orders()):
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
            source="after_recovery_cancel_all",
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


def _is_effectively_flat_qty(sim, qty: float) -> bool:
    return float(qty) <= _qty_step_tolerance(sim)


def _has_non_recovery_active_orders(sim) -> bool:
    return any(
        not str(getattr(order, "purpose", "") or "").startswith("RECOVERY_")
        for order in sim.book.active_orders()
    )


def _has_recovery_active_orders(sim) -> bool:
    return any(
        str(getattr(order, "purpose", "") or "").startswith("RECOVERY_")
        for order in sim.book.active_orders()
    )


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
    return _estimate_combined_exit_loss_usdt(
        sim,
        tracker,
        long_qty=float(qty),
        short_qty=float(qty),
        current_price=float(current_price),
    )


def _estimate_combined_exit_loss_usdt(
    sim,
    tracker: RecoveryBotTracker,
    *,
    long_qty: float,
    short_qty: float,
    current_price: float,
) -> tuple[float, float]:
    config = tracker.config
    if long_qty <= 0 and short_qty <= 0:
        return 0.0, 0.0
    fee_rate = _resolve_fee_rate(sim, config)
    long_fill_price, short_fill_price = _conservative_pair_prices(float(current_price), config)
    long_pnl = 0.0
    short_pnl = 0.0
    if long_qty > 0:
        long_pnl, _ = closed_pnl_for_virtual_order_fill(
            side="long",
            reduce_only=True,
            avg_entry_price=float(sim.book.long_avg or 0.0),
            fill_price=float(long_fill_price),
            qty=float(long_qty),
            fee_rate=fee_rate,
        )
    if short_qty > 0:
        short_pnl, _ = closed_pnl_for_virtual_order_fill(
            side="short",
            reduce_only=True,
            avg_entry_price=float(sim.book.short_avg or 0.0),
            fill_price=float(short_fill_price),
            qty=float(short_qty),
            fee_rate=fee_rate,
        )
    combined = float(long_pnl) + float(short_pnl)
    return max(0.0, -combined), combined


def _validate_full_close_qty(
    sim,
    tracker: RecoveryBotTracker,
    *,
    qty: float,
    current_price: float,
) -> str | None:
    if _is_effectively_flat_qty(sim, qty):
        return None

    rules = _instrument_rules(sim)
    qty_step = float(_rule_decimal(rules, "qty_step", "0.001"))
    min_order_qty = float(_rule_decimal(rules, "min_order_qty", "0.001"))
    min_notional = float(_rule_decimal(rules, "min_notional", "5"))
    tolerance = _step_alignment_tolerance(qty_step)
    if not _is_step_aligned_qty(float(qty), qty_step):
        return "final_exit_untradeable_residual"
    if float(qty) + tolerance < min_order_qty:
        return "final_exit_untradeable_residual"
    conservative_price = float(current_price) * max(0.0, 1.0 - _slippage_buffer_fraction(tracker.config))
    if conservative_price * float(qty) + 1e-12 < min_notional:
        return "final_exit_untradeable_residual"
    return None


def _record_filled_order_state(sim, fill_events: list[FillEvent]) -> None:
    for fill_event in fill_events:
        filled_order = sim.book.get_order(fill_event.client_order_id)
        if filled_order is None:
            continue
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


def _resolve_reload_qty(
    sim,
    *,
    notional_usdt: float,
    fill_price: float,
) -> float:
    strategy = getattr(sim, "strategy", None)
    if strategy is None or not hasattr(strategy, "_price_to_qty"):
        return 0.0
    qty = float(
        strategy._price_to_qty(  # type: ignore[attr-defined]
            notional_usdt=float(notional_usdt),
            price=float(fill_price),
            runtime_state=sim.runtime_state,
        )
    )
    return max(0.0, qty)


def _validate_open_qty(
    sim,
    *,
    qty: float,
    fill_price: float,
) -> str | None:
    rules = _instrument_rules(sim)
    qty_step = float(_rule_decimal(rules, "qty_step", "0.001"))
    min_order_qty = float(_rule_decimal(rules, "min_order_qty", "0.001"))
    min_notional = float(_rule_decimal(rules, "min_notional", "5"))
    tolerance = _step_alignment_tolerance(qty_step)
    rounded_qty = _round_down_to_step(float(qty), qty_step)
    if qty <= 0 or not math.isclose(float(qty), float(rounded_qty), rel_tol=0.0, abs_tol=tolerance):
        return "recovery_reload_untradeable"
    if float(qty) + tolerance < min_order_qty:
        return "recovery_reload_untradeable"
    if float(fill_price) * float(qty) + 1e-12 < min_notional:
        return "recovery_reload_untradeable"
    return None


def _arm_neutralization_after_reload(
    sim,
    tracker: RecoveryBotTracker,
    *,
    current_price: float,
    candle_index: int,
) -> None:
    current_long_qty = float(sim.book.long_qty or 0.0)
    current_short_qty = float(sim.book.short_qty or 0.0)
    tracker.state = RecoveryState.NEUTRALIZING
    tracker.waiting_for_reload_since_candle_index = None
    tracker.recovery_start_price = float(current_price)
    tracker.recovery_start_candle_index = int(candle_index)
    tracker.recovery_start_long_qty = current_long_qty
    tracker.recovery_start_short_qty = current_short_qty
    tracker.neutralization_anchor_price = float(current_price)
    net_long = max(compute_net_long_qty(current_long_qty, current_short_qty), 0.0)
    tracker.neutralization_start_net_long_qty = net_long
    if tracker.config.neutralize_reduce_mode == "fixed_steps":
        target_steps = int(tracker.config.neutralize_target_steps or 1)
        tracker.neutralization_fixed_step_qty = float(net_long) / float(target_steps) if target_steps > 0 else 0.0
    else:
        tracker.neutralization_fixed_step_qty = None
    tracker.remaining_long_qty = current_long_qty
    tracker.remaining_short_qty = current_short_qty
    tracker.last_action_candle_index = int(candle_index)
    tracker.blocked_reason = None
    tracker.reload_reason = "recovery_reload_filled"


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
    candle_index: int | None = None,
) -> bool:
    if tracker.state != RecoveryState.MINIMUM_PAIR_REACHED:
        return False
    state_before = tracker.state
    tracker.state = RecoveryState.READY_TO_CLOSE
    tracker.final_exit_reason = "final_exit_pending"
    tracker.waiting_for_reload_since_candle_index = None
    append_recovery_trace(
        tracker,
        sim=sim,
        action="FINAL_EXIT_EVALUATED",
        reason="minimum_pair_ready_for_exit",
        state_before=state_before,
        state_after=tracker.state,
        candle_index=candle_index,
        current_price=current_price,
    )
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
        append_recovery_trace(
            tracker,
            sim=sim,
            action="RECOVERY_FAILED",
            reason=tracker.blocked_reason,
            state_before=tracker.state,
            state_after=tracker.state,
            candle_index=candle_index,
            current_price=current_price,
        )
        return []

    append_recovery_trace(
        tracker,
        sim=sim,
        action="NEUTRALIZATION_SUBMITTED",
        reason=None,
        state_before=tracker.state,
        state_after=tracker.state,
        candle_index=candle_index,
        current_price=current_price,
    )
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
    append_recovery_trace(
        tracker,
        sim=sim,
        action="NEUTRALIZATION_FILLED",
        reason=None,
        state_before=RecoveryState.NEUTRALIZING,
        state_after=tracker.state,
        candle_index=candle_index,
        current_price=float(fill_event.exec_price),
    )

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
        state_before = tracker.state
        tracker.state = RecoveryState.MINIMUM_PAIR_REACHED
        tracker.minimum_pair_reached = True
        tracker.remaining_long_qty = long_qty
        tracker.remaining_short_qty = short_qty
        tracker.last_action_candle_index = int(candle_index)
        tracker.blocked_reason = None
        append_recovery_trace(
            tracker,
            sim=sim,
            action="MINIMUM_PAIR_REACHED",
            reason="pair_reduce_minimum_hit",
            state_before=state_before,
            state_after=tracker.state,
            candle_index=candle_index,
            current_price=current_price,
        )
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
            state_before = tracker.state
            tracker.state = RecoveryState.MINIMUM_PAIR_REACHED
            tracker.minimum_pair_reached = True
            tracker.remaining_long_qty = float(sim.book.long_qty or 0.0)
            tracker.remaining_short_qty = float(sim.book.short_qty or 0.0)
            tracker.last_action_candle_index = int(candle_index)
            tracker.blocked_reason = None
            append_recovery_trace(
                tracker,
                sim=sim,
                action="MINIMUM_PAIR_REACHED",
                reason="pair_reduce_minimum_hit",
                state_before=state_before,
                state_after=tracker.state,
                candle_index=candle_index,
                current_price=current_price,
            )
        else:
            tracker.state = RecoveryState.FAILED
            tracker.blocked_reason = "pair_reduction_untradeable"
            append_recovery_trace(
                tracker,
                sim=sim,
                action="RECOVERY_FAILED",
                reason=tracker.blocked_reason,
                state_before=RecoveryState.PAIR_REDUCING,
                state_after=tracker.state,
                candle_index=candle_index,
                current_price=current_price,
            )
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
        append_recovery_trace(
            tracker,
            sim=sim,
            action="RECOVERY_FAILED",
            reason=tracker.blocked_reason,
            state_before=RecoveryState.PAIR_REDUCING,
            state_after=tracker.state,
            candle_index=candle_index,
            current_price=current_price,
        )
        return []

    append_recovery_trace(
        tracker,
        sim=sim,
        action="PAIR_REDUCTION_SUBMITTED",
        reason=None,
        state_before=tracker.state,
        state_after=tracker.state,
        candle_index=candle_index,
        current_price=current_price,
    )
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
        append_recovery_trace(
            tracker,
            sim=sim,
            action="RECOVERY_FAILED",
            reason=tracker.blocked_reason,
            state_before=RecoveryState.PAIR_REDUCING,
            state_after=tracker.state,
            candle_index=candle_index,
            current_price=current_price,
        )
        return fill_events

    _record_filled_order_state(sim, fill_events)
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
    append_recovery_trace(
        tracker,
        sim=sim,
        action="PAIR_REDUCTION_FILLED",
        reason=None,
        state_before=RecoveryState.PAIR_REDUCING,
        state_after=tracker.state,
        candle_index=candle_index,
        current_price=float(sim.candle.close),
    )

    if _minimum_pair_reached(sim, tracker, current_price=float(current_price)):
        state_before = tracker.state
        tracker.state = RecoveryState.MINIMUM_PAIR_REACHED
        tracker.minimum_pair_reached = True
        append_recovery_trace(
            tracker,
            sim=sim,
            action="MINIMUM_PAIR_REACHED",
            reason="pair_reduce_minimum_hit",
            state_before=state_before,
            state_after=tracker.state,
            candle_index=candle_index,
            current_price=current_price,
        )

    return fill_events


def maybe_advance_minimum_pair_state(
    sim,
    tracker: RecoveryBotTracker,
    *,
    current_price: float,
    candle_index: int | None = None,
) -> bool:
    return _evaluate_post_minimum_state(
        sim,
        tracker,
        current_price=float(current_price),
        candle_index=candle_index,
    )


def maybe_execute_recovery_final_exit(
    sim,
    tracker: RecoveryBotTracker,
    *,
    current_price: float,
    candle_index: int,
) -> list[FillEvent]:
    if tracker is None or tracker.state != RecoveryState.READY_TO_CLOSE:
        return []

    append_recovery_trace(
        tracker,
        sim=sim,
        action="FINAL_EXIT_EVALUATED",
        reason="ready_to_close",
        state_before=tracker.state,
        state_after=tracker.state,
        candle_index=candle_index,
        current_price=current_price,
    )
    ensure_recovery_exclusive_order_state(sim, tracker)
    _cancel_conflicting_active_orders(sim)
    tracker.final_exit_attempted = True
    tracker.final_exit_candle_index = int(candle_index)
    tracker.final_exit_combined_pnl = 0.0

    long_qty = float(sim.book.long_qty or 0.0)
    short_qty = float(sim.book.short_qty or 0.0)
    tracker.final_exit_long_qty = long_qty
    tracker.final_exit_short_qty = short_qty
    tracker.remaining_long_qty = long_qty
    tracker.remaining_short_qty = short_qty
    tracker.last_action_candle_index = int(candle_index)

    if _is_effectively_flat_qty(sim, long_qty) and _is_effectively_flat_qty(sim, short_qty):
        _cancel_all_active_orders(sim)
        tracker.state = RecoveryState.CLOSED
        tracker.final_exit_reason = "already_flat"
        tracker.blocked_reason = None
        tracker.remaining_long_qty = 0.0
        tracker.remaining_short_qty = 0.0
        append_recovery_trace(
            tracker,
            sim=sim,
            action="RECOVERY_CLOSED",
            reason=tracker.final_exit_reason,
            state_before=RecoveryState.READY_TO_CLOSE,
            state_after=tracker.state,
            candle_index=candle_index,
            current_price=current_price,
        )
        return []

    if _has_non_recovery_active_orders(sim):
        tracker.state = RecoveryState.FAILED
        tracker.blocked_reason = "final_exit_conflicting_orders_remaining"
        append_recovery_trace(
            tracker,
            sim=sim,
            action="RECOVERY_FAILED",
            reason=tracker.blocked_reason,
            state_before=RecoveryState.READY_TO_CLOSE,
            state_after=tracker.state,
            candle_index=candle_index,
            current_price=current_price,
        )
        return []

    if _is_effectively_flat_qty(sim, long_qty) or _is_effectively_flat_qty(sim, short_qty):
        tracker.state = RecoveryState.FAILED
        tracker.blocked_reason = "final_exit_untradeable_residual"
        tracker.final_exit_reason = "partial_final_exit"
        append_recovery_trace(
            tracker,
            sim=sim,
            action="RECOVERY_FAILED",
            reason=tracker.blocked_reason,
            state_before=RecoveryState.READY_TO_CLOSE,
            state_after=tracker.state,
            candle_index=candle_index,
            current_price=current_price,
        )
        return []

    long_reason = _validate_full_close_qty(
        sim,
        tracker,
        qty=long_qty,
        current_price=float(current_price),
    )
    short_reason = _validate_full_close_qty(
        sim,
        tracker,
        qty=short_qty,
        current_price=float(current_price),
    )
    if long_reason or short_reason:
        tracker.state = RecoveryState.FAILED
        tracker.blocked_reason = "final_exit_untradeable_residual"
        tracker.final_exit_reason = "final_exit_untradeable_residual"
        append_recovery_trace(
            tracker,
            sim=sim,
            action="RECOVERY_FAILED",
            reason=tracker.blocked_reason,
            state_before=RecoveryState.READY_TO_CLOSE,
            state_after=tracker.state,
            candle_index=candle_index,
            current_price=current_price,
        )
        return []

    expected_loss, _combined_estimate = _estimate_combined_exit_loss_usdt(
        sim,
        tracker,
        long_qty=long_qty,
        short_qty=short_qty,
        current_price=float(current_price),
    )
    if would_exceed_loss_budget(
        tracker.loss_budget_usdt,
        tracker.loss_budget_used_usdt,
        expected_loss,
    ):
        tracker.blocked_reason = "final_exit_blocked_by_loss_budget"
        _mark_waiting_for_reload(
            tracker,
            candle_index=candle_index,
            reason="final_exit_outside_loss_budget",
        )
        tracker.blocked_reason = "final_exit_blocked_by_loss_budget"
        append_recovery_trace(
            tracker,
            sim=sim,
            action="FINAL_EXIT_BLOCKED",
            reason=tracker.blocked_reason,
            state_before=RecoveryState.READY_TO_CLOSE,
            state_after=tracker.state,
            candle_index=candle_index,
            current_price=current_price,
        )
        append_recovery_trace(
            tracker,
            sim=sim,
            action="RELOAD_WAITING",
            reason=tracker.final_exit_reason,
            state_before=RecoveryState.READY_TO_CLOSE,
            state_after=tracker.state,
            candle_index=candle_index,
            current_price=current_price,
        )
        return []

    metadata = _recovery_metadata(sim, tracker)
    long_intent = StrategyIntent(
        side="long",
        qty=long_qty,
        purpose=RECOVERY_FINAL_EXIT_LONG_PURPOSE,
        order_type="Market",
        reduce_only=True,
        position_idx=1,
        metadata=dict(metadata),
    )
    short_intent = StrategyIntent(
        side="short",
        qty=short_qty,
        purpose=RECOVERY_FINAL_EXIT_SHORT_PURPOSE,
        order_type="Market",
        reduce_only=True,
        position_idx=2,
        metadata=dict(metadata),
    )
    long_idx = sim._log_intent(long_intent, event_source="recovery_final_exit")
    short_idx = sim._log_intent(short_intent, event_source="recovery_final_exit")
    long_order = sim._submit_intent_with_logging(long_intent, replace=False, intent_log_index=long_idx)
    short_order = sim._submit_intent_with_logging(short_intent, replace=False, intent_log_index=short_idx)
    if long_order is None or short_order is None:
        tracker.state = RecoveryState.FAILED
        tracker.blocked_reason = "final_exit_atomicity_failed"
        tracker.final_exit_reason = "partial_final_exit"
        append_recovery_trace(
            tracker,
            sim=sim,
            action="RECOVERY_FAILED",
            reason=tracker.blocked_reason,
            state_before=RecoveryState.READY_TO_CLOSE,
            state_after=tracker.state,
            candle_index=candle_index,
            current_price=current_price,
        )
        return []

    append_recovery_trace(
        tracker,
        sim=sim,
        action="FINAL_EXIT_SUBMITTED",
        reason=None,
        state_before=tracker.state,
        state_after=tracker.state,
        candle_index=candle_index,
        current_price=current_price,
    )
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
        tracker.blocked_reason = "final_exit_atomicity_failed"
        tracker.final_exit_reason = "partial_final_exit"
        append_recovery_trace(
            tracker,
            sim=sim,
            action="RECOVERY_FAILED",
            reason=tracker.blocked_reason,
            state_before=RecoveryState.READY_TO_CLOSE,
            state_after=tracker.state,
            candle_index=candle_index,
            current_price=current_price,
        )
        return fill_events

    _record_filled_order_state(sim, fill_events)
    sim.book.sync_runtime_state(sim.runtime_state)
    sim._refresh_snapshot_from_book(
        source="after_recovery_final_exit_fill",
        price=sim.candle.close,
    )

    combined_closed_pnl = sum(float((fill.metadata or {}).get("closed_pnl") or 0.0) for fill in fill_events)
    tracker.final_exit_combined_pnl = combined_closed_pnl
    if combined_closed_pnl < 0:
        tracker.loss_budget_used_usdt += abs(combined_closed_pnl)
    tracker.recovery_realized_pnl += combined_closed_pnl
    tracker.remaining_long_qty = float(sim.book.long_qty or 0.0)
    tracker.remaining_short_qty = float(sim.book.short_qty or 0.0)
    tracker.last_action_candle_index = int(candle_index)
    tracker.blocked_reason = None
    append_recovery_trace(
        tracker,
        sim=sim,
        action="FINAL_EXIT_FILLED",
        reason=None,
        state_before=RecoveryState.READY_TO_CLOSE,
        state_after=tracker.state,
        candle_index=candle_index,
        current_price=float(sim.candle.close),
    )

    _cancel_all_active_orders(sim)
    long_remaining = float(sim.book.long_qty or 0.0)
    short_remaining = float(sim.book.short_qty or 0.0)
    if (
        _is_effectively_flat_qty(sim, long_remaining)
        and _is_effectively_flat_qty(sim, short_remaining)
        and not sim.book.active_orders()
    ):
        tracker.state = RecoveryState.CLOSED
        tracker.final_exit_reason = "recovery_full_exit_within_budget"
        tracker.remaining_long_qty = 0.0
        tracker.remaining_short_qty = 0.0
        append_recovery_trace(
            tracker,
            sim=sim,
            action="RECOVERY_CLOSED",
            reason=tracker.final_exit_reason,
            state_before=RecoveryState.READY_TO_CLOSE,
            state_after=tracker.state,
            candle_index=candle_index,
            current_price=current_price,
        )
        return fill_events

    tracker.state = RecoveryState.FAILED
    tracker.blocked_reason = "final_exit_untradeable_residual"
    tracker.final_exit_reason = "partial_final_exit"
    tracker.remaining_long_qty = long_remaining
    tracker.remaining_short_qty = short_remaining
    append_recovery_trace(
        tracker,
        sim=sim,
        action="RECOVERY_FAILED",
        reason=tracker.blocked_reason,
        state_before=RecoveryState.READY_TO_CLOSE,
        state_after=tracker.state,
        candle_index=candle_index,
        current_price=current_price,
    )
    return fill_events


def maybe_execute_recovery_reload(
    sim,
    tracker: RecoveryBotTracker,
    *,
    current_price: float,
    candle_index: int,
) -> list[FillEvent]:
    if tracker is None or tracker.state != RecoveryState.WAITING_FOR_RELOAD:
        return []

    ensure_recovery_exclusive_order_state(sim, tracker)
    if not bool(tracker.config.reload_enabled):
        return []

    tracker.reload_attempted = False

    if tracker.reload_count >= int(tracker.config.max_reloads_per_trade or 0):
        tracker.blocked_reason = "max_recovery_reloads_reached"
        append_recovery_trace(
            tracker,
            sim=sim,
            action="RELOAD_WAITING",
            reason=tracker.blocked_reason,
            state_before=tracker.state,
            state_after=tracker.state,
            candle_index=candle_index,
            current_price=current_price,
        )
        return []

    wait_start = tracker.waiting_for_reload_since_candle_index
    if wait_start is None:
        inferred = tracker.last_action_candle_index
        tracker.waiting_for_reload_since_candle_index = (
            int(inferred) if inferred is not None else int(candle_index)
        )
        wait_start = tracker.waiting_for_reload_since_candle_index
    if int(candle_index) - int(wait_start) < int(tracker.config.reload_wait_candles or 0):
        tracker.blocked_reason = None
        append_recovery_trace(
            tracker,
            sim=sim,
            action="RELOAD_WAITING",
            reason="reload_wait_candles_not_reached",
            state_before=tracker.state,
            state_after=tracker.state,
            candle_index=candle_index,
            current_price=current_price,
        )
        return []

    _cancel_conflicting_active_orders(sim)
    if _has_non_recovery_active_orders(sim):
        tracker.state = RecoveryState.FAILED
        tracker.blocked_reason = "recovery_reload_conflicting_orders_remaining"
        tracker.reload_reason = "conflicting_orders"
        append_recovery_trace(
            tracker,
            sim=sim,
            action="RECOVERY_FAILED",
            reason=tracker.blocked_reason,
            state_before=RecoveryState.WAITING_FOR_RELOAD,
            state_after=tracker.state,
            candle_index=candle_index,
            current_price=current_price,
        )
        return []
    if _has_recovery_active_orders(sim):
        tracker.state = RecoveryState.FAILED
        tracker.blocked_reason = "recovery_reload_active_order_conflict"
        tracker.reload_reason = "active_recovery_order_present"
        append_recovery_trace(
            tracker,
            sim=sim,
            action="RECOVERY_FAILED",
            reason=tracker.blocked_reason,
            state_before=RecoveryState.WAITING_FOR_RELOAD,
            state_after=tracker.state,
            candle_index=candle_index,
            current_price=current_price,
        )
        return []

    long_current_qty = float(sim.book.long_qty or 0.0)
    short_current_qty = float(sim.book.short_qty or 0.0)
    long_avg = float(sim.book.long_avg or 0.0)
    short_avg = float(sim.book.short_avg or 0.0)
    if long_current_qty <= 0 or short_current_qty <= 0 or long_avg <= 0 or short_avg <= 0:
        tracker.state = RecoveryState.FAILED
        tracker.blocked_reason = "recovery_reload_invalid_position_state"
        tracker.reload_reason = "invalid_position_state"
        append_recovery_trace(
            tracker,
            sim=sim,
            action="RECOVERY_FAILED",
            reason=tracker.blocked_reason,
            state_before=RecoveryState.WAITING_FOR_RELOAD,
            state_after=tracker.state,
            candle_index=candle_index,
            current_price=current_price,
        )
        return []

    long_fill_price, short_fill_price = _reload_fill_prices(float(current_price), tracker.config)
    long_notional = float(tracker.config.reload_long_notional_usdt or 0.0)
    short_notional = float(tracker.config.reload_short_notional_usdt or 0.0)
    long_qty = _resolve_reload_qty(
        sim,
        notional_usdt=long_notional,
        fill_price=long_fill_price,
    )
    short_qty = _resolve_reload_qty(
        sim,
        notional_usdt=short_notional,
        fill_price=short_fill_price,
    )

    if long_qty <= 0 or short_qty <= 0:
        tracker.state = RecoveryState.FAILED
        tracker.blocked_reason = "recovery_reload_untradeable"
        tracker.reload_reason = "untradeable_reload_qty"
        append_recovery_trace(
            tracker,
            sim=sim,
            action="RECOVERY_FAILED",
            reason=tracker.blocked_reason,
            state_before=RecoveryState.WAITING_FOR_RELOAD,
            state_after=tracker.state,
            candle_index=candle_index,
            current_price=current_price,
        )
        return []

    long_reason = _validate_open_qty(
        sim,
        qty=long_qty,
        fill_price=long_fill_price,
    )
    short_reason = _validate_open_qty(
        sim,
        qty=short_qty,
        fill_price=short_fill_price,
    )
    if long_reason or short_reason:
        tracker.state = RecoveryState.FAILED
        tracker.blocked_reason = "recovery_reload_untradeable"
        tracker.reload_reason = "untradeable_reload_qty"
        append_recovery_trace(
            tracker,
            sim=sim,
            action="RECOVERY_FAILED",
            reason=tracker.blocked_reason,
            state_before=RecoveryState.WAITING_FOR_RELOAD,
            state_after=tracker.state,
            candle_index=candle_index,
            current_price=current_price,
        )
        return []

    planned_total_notional = float(long_fill_price * long_qty) + float(short_fill_price * short_qty)
    max_total = tracker.config.reload_max_total_notional_usdt
    if max_total is not None and float(max_total) > 0 and planned_total_notional > float(max_total) + 1e-12:
        tracker.state = RecoveryState.FAILED
        tracker.blocked_reason = "recovery_reload_notional_limit_exceeded"
        tracker.reload_reason = "notional_limit_exceeded"
        append_recovery_trace(
            tracker,
            sim=sim,
            action="RECOVERY_FAILED",
            reason=tracker.blocked_reason,
            state_before=RecoveryState.WAITING_FOR_RELOAD,
            state_after=tracker.state,
            candle_index=candle_index,
            current_price=current_price,
        )
        return []

    tracker.reload_attempted = True
    tracker.reload_candle_index = int(candle_index)
    tracker.reload_long_qty = float(long_qty)
    tracker.reload_short_qty = float(short_qty)
    tracker.reload_reason = "reload_submitted"

    metadata = _recovery_metadata(sim, tracker)
    metadata.update(
        {
            "reload_count": int(tracker.reload_count + 1),
            "reload_long_notional_usdt": long_notional,
            "reload_short_notional_usdt": short_notional,
            "reload_fill_reference_price": float(current_price),
            "reload_long_fill_price": float(long_fill_price),
            "reload_short_fill_price": float(short_fill_price),
            "reload_reason": tracker.reload_reason,
        }
    )
    long_intent = StrategyIntent(
        side="long",
        qty=float(long_qty),
        purpose=RECOVERY_RELOAD_LONG_PURPOSE,
        order_type="Market",
        reduce_only=False,
        position_idx=1,
        metadata=dict(metadata),
    )
    short_intent = StrategyIntent(
        side="short",
        qty=float(short_qty),
        purpose=RECOVERY_RELOAD_SHORT_PURPOSE,
        order_type="Market",
        reduce_only=False,
        position_idx=2,
        metadata=dict(metadata),
    )
    long_idx = sim._log_intent(long_intent, event_source="recovery_reload")
    short_idx = sim._log_intent(short_intent, event_source="recovery_reload")
    long_order = sim._submit_intent_with_logging(long_intent, replace=False, intent_log_index=long_idx)
    short_order = sim._submit_intent_with_logging(short_intent, replace=False, intent_log_index=short_idx)
    if long_order is None or short_order is None:
        tracker.state = RecoveryState.FAILED
        tracker.blocked_reason = "recovery_reload_atomicity_failed"
        tracker.reload_reason = "partial_reload"
        append_recovery_trace(
            tracker,
            sim=sim,
            action="RECOVERY_FAILED",
            reason=tracker.blocked_reason,
            state_before=RecoveryState.WAITING_FOR_RELOAD,
            state_after=tracker.state,
            candle_index=candle_index,
            current_price=current_price,
        )
        return []

    append_recovery_trace(
        tracker,
        sim=sim,
        action="RELOAD_SUBMITTED",
        reason=None,
        state_before=tracker.state,
        state_after=tracker.state,
        candle_index=candle_index,
        current_price=current_price,
    )
    sim.orders_submitted += 2
    fill_events: list[FillEvent] = []
    try:
        fill_events.append(
            fill_order_at_price(
                book=sim.book,
                runtime_state=sim.runtime_state,
                order_id=long_order.order_id,
                fill_price=float(long_fill_price),
                occurred_at=sim.candle.timestamp,
                touch_metadata={"order_check_price": float(current_price)},
            )
        )
        fill_events.append(
            fill_order_at_price(
                book=sim.book,
                runtime_state=sim.runtime_state,
                order_id=short_order.order_id,
                fill_price=float(short_fill_price),
                occurred_at=sim.candle.timestamp,
                touch_metadata={"order_check_price": float(current_price)},
            )
        )
    except Exception:
        tracker.state = RecoveryState.FAILED
        tracker.blocked_reason = "recovery_reload_atomicity_failed"
        tracker.reload_reason = "partial_reload"
        append_recovery_trace(
            tracker,
            sim=sim,
            action="RECOVERY_FAILED",
            reason=tracker.blocked_reason,
            state_before=RecoveryState.WAITING_FOR_RELOAD,
            state_after=tracker.state,
            candle_index=candle_index,
            current_price=current_price,
        )
        return fill_events

    _record_filled_order_state(sim, fill_events)
    sim.book.sync_runtime_state(sim.runtime_state)
    sim._refresh_snapshot_from_book(
        source="after_recovery_reload_fill",
        price=sim.candle.close,
    )

    tracker.reload_count += 1
    tracker.reload_reason = "recovery_reload_filled"
    tracker.remaining_long_qty = float(sim.book.long_qty or 0.0)
    tracker.remaining_short_qty = float(sim.book.short_qty or 0.0)
    _arm_neutralization_after_reload(
        sim,
        tracker,
        current_price=float(current_price),
        candle_index=candle_index,
    )
    append_recovery_trace(
        tracker,
        sim=sim,
        action="RELOAD_FILLED",
        reason=tracker.reload_reason,
        state_before=RecoveryState.WAITING_FOR_RELOAD,
        state_after=tracker.state,
        candle_index=candle_index,
        current_price=current_price,
    )
    return fill_events


def collect_recovery_invariant_violations(
    sim,
    tracker: RecoveryBotTracker | None,
) -> list[str]:
    if tracker is None:
        return []

    violations: list[str] = []
    long_qty = float(sim.book.long_qty or 0.0)
    short_qty = float(sim.book.short_qty or 0.0)
    active_orders = list(sim.book.active_orders())
    active_recovery = [
        order for order in active_orders if str(getattr(order, "purpose", "") or "").startswith("RECOVERY_")
    ]
    active_normal = [
        order for order in active_orders if not str(getattr(order, "purpose", "") or "").startswith("RECOVERY_")
    ]

    if tracker.state == RecoveryState.CLOSED:
        if not _is_effectively_flat_qty(sim, long_qty):
            violations.append("closed_state_with_open_long_qty")
        if not _is_effectively_flat_qty(sim, short_qty):
            violations.append("closed_state_with_open_short_qty")
        if active_orders:
            violations.append("closed_state_with_active_orders")

    if tracker.state in RECOVERY_FROZEN_STATES and active_normal:
        violations.append("frozen_recovery_state_with_normal_orders")

    if tracker.state == RecoveryState.FAILED and active_recovery:
        violations.append("failed_state_with_active_recovery_orders")

    if tracker.reload_count > int(tracker.config.max_reloads_per_trade or 0):
        violations.append("reload_count_exceeds_max")

    if tracker.loss_budget_used_usdt < 0:
        violations.append("negative_loss_budget_used")

    return violations


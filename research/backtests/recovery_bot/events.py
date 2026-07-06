from __future__ import annotations

from typing import Any, Iterable

from .calculations import (
    compute_loss_budget_usdt,
    compute_neutralization_fixed_step_qty,
    compute_net_long_qty,
    compute_price_drop_pct,
    extract_cycle_index,
    matches_configured_trigger,
)
from .config import RecoveryBotConfig
from .state import RecoveryBotTracker, RecoveryState


def attach_recovery_bot_tracker(
    config: RecoveryBotConfig | None,
) -> RecoveryBotTracker | None:
    """Return a tracker for an enabled config, otherwise None.

    When the config is disabled or missing the recovery bot is inert and the
    caller must not change any existing simulator/backtest behaviour.
    """
    if config is None or not config.enabled:
        return None
    return RecoveryBotTracker(config=config)


def observe_recovery_trigger_fills(
    tracker: RecoveryBotTracker,
    *,
    fills: Iterable[Any],
    candle_index: int,
) -> bool:
    """Inspect filled events for the configured trigger purpose.

    Only actual fills (not resting/active orders) should be passed here. When a
    matching purpose is seen for the first time the tracker records trigger
    details and moves into TRIGGER_OBSERVED.
    """
    if tracker is None or not tracker.config.enabled:
        return False
    if tracker.state != RecoveryState.WAITING_FOR_TRIGGER:
        # Trigger can only be recorded once per run.
        return False

    configured = tracker.config.trigger_order
    for fill in fills:
        purpose = getattr(fill, "purpose", None)
        if not matches_configured_trigger(purpose, configured):
            continue

        # Guard against double-counting even if the caller accidentally passes
        # the same fills more than once for the same candle.
        if tracker.trigger_candle_index is not None:
            return False

        price = getattr(fill, "exec_price", None)
        try:
            price_value = float(price) if price is not None else 0.0
        except (TypeError, ValueError):
            price_value = 0.0

        tracker.trigger_purpose = str(purpose or "")
        tracker.trigger_cycle_index = extract_cycle_index(purpose)
        tracker.trigger_fill_price = price_value
        tracker.trigger_candle_index = int(candle_index)
        tracker.state = RecoveryState.TRIGGER_OBSERVED
        tracker.last_action_candle_index = int(candle_index)
        return True

    return False


def maybe_activate_recovery(
    tracker: RecoveryBotTracker,
    *,
    current_price: float,
    candle_index: int,
    current_long_qty: float,
    current_short_qty: float,
) -> bool:
    """Transition TRIGGER_OBSERVED -> NEUTRALIZING when conditions are met.

    This function is purely stateful: it must not submit orders, generate
    fills, or otherwise change simulator/backtest behaviour beyond updating
    the tracker.
    """
    if tracker is None or not tracker.config.enabled:
        return False
    if tracker.state != RecoveryState.TRIGGER_OBSERVED:
        return False

    config = tracker.config

    # Enforce maximum number of recovery runs per trade.
    if tracker.recovery_runs_for_trade >= int(config.max_recovery_runs_per_trade or 0):
        return False

    if tracker.trigger_candle_index is None:
        return False
    trigger_candle = int(tracker.trigger_candle_index)
    candles_since_trigger = int(candle_index) - trigger_candle
    if candles_since_trigger < int(config.trigger_wait_candles or 0):
        return False

    trigger_price = float(tracker.trigger_fill_price or 0.0)
    price_drop_pct = compute_price_drop_pct(float(current_price), trigger_price)
    required_drop = float(config.trigger_price_drop_pct or 0.0)
    if required_drop > 0.0 and price_drop_pct < required_drop:
        return False

    # Preconditions satisfied: activate NEUTRALIZING without touching orders.
    tracker.state = RecoveryState.NEUTRALIZING
    tracker.recovery_runs_for_trade += 1
    tracker.recovery_start_price = float(current_price)
    tracker.recovery_start_candle_index = int(candle_index)
    tracker.recovery_start_long_qty = float(current_long_qty)
    tracker.recovery_start_short_qty = float(current_short_qty)
    tracker.neutralization_anchor_price = float(current_price)

    net_long = max(
        compute_net_long_qty(float(current_long_qty), float(current_short_qty)),
        0.0,
    )
    tracker.neutralization_start_net_long_qty = net_long
    tracker.neutralization_fixed_step_qty = compute_neutralization_fixed_step_qty(
        net_long,
        int(config.neutralize_target_steps or 1),
    )

    tracker.loss_budget_usdt = compute_loss_budget_usdt(config)
    tracker.loss_budget_used_usdt = 0.0
    tracker.last_action_candle_index = int(candle_index)
    return True


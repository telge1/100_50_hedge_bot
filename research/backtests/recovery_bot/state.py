from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .config import RecoveryBotConfig


class RecoveryState(str, Enum):
    DISABLED = "DISABLED"
    WAITING_FOR_TRIGGER = "WAITING_FOR_TRIGGER"
    TRIGGER_OBSERVED = "TRIGGER_OBSERVED"
    NEUTRALIZING = "NEUTRALIZING"
    PAIR_REDUCING = "PAIR_REDUCING"
    MINIMUM_PAIR_REACHED = "MINIMUM_PAIR_REACHED"
    READY_TO_CLOSE = "READY_TO_CLOSE"
    WAITING_FOR_RELOAD = "WAITING_FOR_RELOAD"
    CLOSED = "CLOSED"
    FAILED = "FAILED"


@dataclass
class RecoveryBotTracker:
    """Per-trade state for the backtest-only recovery bot.

    This dataclass is intentionally independent from the simulator and can be
    instantiated in isolation for unit testing.
    """

    config: RecoveryBotConfig
    state: RecoveryState = field(default=RecoveryState.DISABLED)
    recovery_runs_for_trade: int = 0

    trigger_purpose: str | None = None
    trigger_cycle_index: int | None = None
    trigger_fill_price: float | None = None
    trigger_candle_index: int | None = None

    recovery_start_price: float | None = None
    recovery_start_candle_index: int | None = None
    recovery_start_long_qty: float | None = None
    recovery_start_short_qty: float | None = None

    neutralization_anchor_price: float | None = None
    neutralization_start_net_long_qty: float | None = None
    neutralization_fixed_step_qty: float | None = None
    neutralization_steps_done: int = 0

    pair_anchor_price: float | None = None
    pair_reduction_steps_done: int = 0

    loss_budget_usdt: float | None = None
    loss_budget_used_usdt: float = 0.0
    pair_reduction_realized_pnl: float = 0.0
    recovery_realized_pnl: float = 0.0

    minimum_pair_reached: bool = False
    final_exit_reason: str | None = None

    remaining_long_qty: float | None = None
    remaining_short_qty: float | None = None
    last_action_candle_index: int | None = None
    blocked_reason: str | None = None

    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # When disabled, keep the tracker in a clearly inert state.
        if not self.config.enabled:
            self.state = RecoveryState.DISABLED
        elif self.state == RecoveryState.DISABLED:
            # When enabled but no explicit state was provided, start waiting
            # for a trigger.
            self.state = RecoveryState.WAITING_FOR_TRIGGER


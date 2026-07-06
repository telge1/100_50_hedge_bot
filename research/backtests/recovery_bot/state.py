from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
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
    final_exit_attempted: bool = False
    final_exit_candle_index: int | None = None
    final_exit_combined_pnl: float = 0.0
    final_exit_long_qty: float = 0.0
    final_exit_short_qty: float = 0.0
    waiting_for_reload_since_candle_index: int | None = None
    reload_count: int = 0
    reload_attempted: bool = False
    reload_candle_index: int | None = None
    reload_long_qty: float = 0.0
    reload_short_qty: float = 0.0
    reload_reason: str | None = None

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


def append_recovery_trace(
    tracker: RecoveryBotTracker | None,
    *,
    sim: Any,
    action: str,
    reason: str | None = None,
    state_before: RecoveryState | str | None = None,
    state_after: RecoveryState | str | None = None,
    candle_index: int | None = None,
    timestamp: datetime | None = None,
    current_price: float | None = None,
) -> None:
    if tracker is None:
        return
    strategy_state = dict(getattr(getattr(sim, "runtime_state", None), "strategy_state", {}) or {})
    trade_id = (
        strategy_state.get("trade_block_id")
        or strategy_state.get("active_trade_block_id")
        or tracker.extra.get("trade_id")
        or f"{getattr(sim, 'symbol', 'UNKNOWN')}:{id(tracker)}"
    )
    tracker.extra["trade_id"] = trade_id

    candle = getattr(sim, "candle", None)
    trace = tracker.extra.setdefault("recovery_trace", [])
    trace.append(
        {
            "trade_id": trade_id,
            "candle_index": int(candle_index) if candle_index is not None else getattr(sim, "candle_index", None),
            "timestamp": (
                timestamp or getattr(candle, "timestamp", None)
            ).isoformat()
            if (timestamp or getattr(candle, "timestamp", None)) is not None
            else None,
            "state_before": str(state_before or tracker.state),
            "state_after": str(state_after or tracker.state),
            "action": str(action),
            "reason": reason,
            "current_price": float(current_price) if current_price is not None else float(getattr(candle, "close", 0.0) or 0.0),
            "long_qty": float(getattr(getattr(sim, "book", None), "long_qty", 0.0) or 0.0),
            "short_qty": float(getattr(getattr(sim, "book", None), "short_qty", 0.0) or 0.0),
            "long_avg": float(getattr(getattr(sim, "book", None), "long_avg", 0.0) or 0.0),
            "short_avg": float(getattr(getattr(sim, "book", None), "short_avg", 0.0) or 0.0),
            "loss_budget_usdt": tracker.loss_budget_usdt,
            "loss_budget_used_usdt": tracker.loss_budget_used_usdt,
            "reload_count": int(tracker.reload_count),
            "active_order_count": len(getattr(getattr(sim, "book", None), "active_orders", lambda: [])()),
        }
    )


def recovery_trace_entries(tracker: RecoveryBotTracker | None) -> list[dict[str, Any]]:
    if tracker is None:
        return []
    return list(tracker.extra.get("recovery_trace") or [])


def build_recovery_summary(
    tracker: RecoveryBotTracker | None,
    *,
    active_orders_remaining: int,
) -> dict[str, Any]:
    if tracker is None:
        return {}
    trace = recovery_trace_entries(tracker)
    start = trace[0] if trace else {}
    end = trace[-1] if trace else {}
    return {
        "trade_id": tracker.extra.get("trade_id"),
        "start_candle_index": start.get("candle_index"),
        "end_candle_index": end.get("candle_index"),
        "start_timestamp": start.get("timestamp"),
        "end_timestamp": end.get("timestamp"),
        "final_state": str(tracker.state),
        "recovery_candles": len({entry.get("candle_index") for entry in trace if entry.get("candle_index") is not None}),
        "neutralization_count": int(tracker.neutralization_steps_done),
        "pair_reduction_count": int(tracker.pair_reduction_steps_done),
        "reload_count": int(tracker.reload_count),
        "final_exit_attempted": bool(tracker.final_exit_attempted),
        "final_exit_reason": tracker.final_exit_reason,
        "blocked_reason": tracker.blocked_reason,
        "loss_budget_usdt": tracker.loss_budget_usdt,
        "loss_budget_used_usdt": tracker.loss_budget_used_usdt,
        "recovery_realized_pnl": tracker.recovery_realized_pnl,
        "final_exit_combined_pnl": tracker.final_exit_combined_pnl,
        "remaining_long_qty": tracker.remaining_long_qty,
        "remaining_short_qty": tracker.remaining_short_qty,
        "active_orders_remaining": int(active_orders_remaining),
    }


from __future__ import annotations

"""Runtime integration for backtest-only long-gap recovery inside historical backtests."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from fixed_cycle_hedge_bot.models import FillEvent

from .long_gap_reduction import LongGapReductionRuntime
from .recovery_bot_config import RecoveryBotConfig, to_long_gap_reduction_config

if TYPE_CHECKING:
    from .backtest_report import BacktestResult
    from .hedge_bot_original_simulator import HedgeBotOriginalSimulator


RECOVERY_DIAGNOSTIC_PURPOSES = (
    "RECOVERY_REFERENCE_REACHED",
    "RECOVERY_WAIT_STARTED",
    "RECOVERY_ACTIVATED",
    "RECOVERY_GAP_REDUCE_STEP_1",
    "RECOVERY_GAP_REDUCE_STEP_2",
    "RECOVERY_GAP_REDUCE_STEP_3",
    "RECOVERY_GAP_REDUCE_STEP_4",
    "RECOVERY_GAP_CLOSED",
    "RECOVERY_JOINT_EXIT",
)


@dataclass
class RecoveryBotState:
    reference_reached: bool = False
    reference_absolute_candle_index: int | None = None
    reference_local_candle_index: int | None = None
    reference_timestamp: str | None = None
    activation_absolute_candle_index: int | None = None
    activation_local_candle_index: int | None = None
    activation_timestamp: str | None = None
    recovery_activated: bool = False
    recovery_completed: bool = False
    recovery_mode_active: bool = False
    wait_started_recorded: bool = False
    gap_runtime: LongGapReductionRuntime | None = None
    recovery_exit_absolute_candle_index: int | None = None
    recovery_exit_timestamp: str | None = None
    recovery_exit_local_candle_index: int | None = None
    initial_gap_qty: float | None = None
    total_reduced_qty: float = 0.0
    remaining_gap_qty: float | None = None
    gap_fully_closed: bool = False
    recovery_total_gap_reduction_net_pnl: float = 0.0
    recovery_final_pnl: float | None = None


@dataclass
class RecoveryBotTracker:
    config: RecoveryBotConfig
    state: RecoveryBotState = field(default_factory=RecoveryBotState)
    diagnostic_events: list[dict[str, Any]] = field(default_factory=list)


def attach_recovery_bot_tracker(
    sim: "HedgeBotOriginalSimulator",
    config: RecoveryBotConfig | None,
) -> RecoveryBotTracker | None:
    if config is None or not config.enabled:
        return None
    tracker = RecoveryBotTracker(config=config)
    if config.stop_new_cycle_orders_on_activation:
        sim.intent_filter = _recovery_intent_filter_factory(tracker)
    setattr(sim, "recovery_bot_tracker", tracker)
    return tracker


def _recovery_intent_filter_factory(tracker: RecoveryBotTracker):
    def _filter(_intent) -> bool:
        return not tracker.state.recovery_mode_active

    return _filter


def trade_absolute_candle_index(
    *,
    input_slice_start_index: int,
    absolute_trade_start_index: int,
    local_candle_index: int,
) -> int:
    return int(input_slice_start_index) + int(absolute_trade_start_index) + int(local_candle_index)


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts is not None else None


def _append_diagnostic(
    tracker: RecoveryBotTracker,
    *,
    purpose: str,
    candle_index: int,
    absolute_candle_index: int,
    timestamp: str | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    tracker.diagnostic_events.append(
        {
            "row_type": "diagnostic",
            "purpose": purpose,
            "candle_index": candle_index,
            "absolute_candle_index": absolute_candle_index,
            "timestamp": timestamp,
            "metadata": dict(metadata or {}),
        }
    )


def _resolve_fee_rate(sim: "HedgeBotOriginalSimulator") -> float | None:
    book_rate = getattr(sim.book, "fee_rate", None)
    if book_rate is not None:
        try:
            return float(book_rate)
        except (TypeError, ValueError):
            pass
    try:
        return float(sim.config.order_fee_rate_pct) / 100.0
    except (TypeError, ValueError, AttributeError):
        return 0.00055


def _cancel_open_orders(sim: "HedgeBotOriginalSimulator") -> None:
    for order in list(sim.book.active_orders()):
        sim.book.cancel_by_order_id(order.order_id)


def _note_reference_from_fills(
    tracker: RecoveryBotTracker,
    *,
    fills: list[FillEvent],
    local_candle_index: int,
    absolute_candle_index: int,
    timestamp: str | None,
) -> None:
    if tracker.state.reference_reached:
        return
    purpose = tracker.config.recovery_start_purpose
    for fill in fills:
        metadata = dict(fill.metadata or {})
        fill_purpose = str(
            getattr(fill, "purpose", None)
            or metadata.get("purpose")
            or metadata.get("bot_purpose")
            or ""
        )
        if fill_purpose != purpose:
            continue
        tracker.state.reference_reached = True
        tracker.state.reference_absolute_candle_index = absolute_candle_index
        tracker.state.reference_local_candle_index = local_candle_index
        tracker.state.reference_timestamp = timestamp
        tracker.state.activation_absolute_candle_index = (
            absolute_candle_index + max(0, int(tracker.config.recovery_wait_candles))
        )
        _append_diagnostic(
            tracker,
            purpose="RECOVERY_REFERENCE_REACHED",
            candle_index=local_candle_index,
            absolute_candle_index=absolute_candle_index,
            timestamp=timestamp,
            metadata={
                "recovery_start_purpose": purpose,
                "recovery_wait_candles": tracker.config.recovery_wait_candles,
                "recovery_activation_absolute_candle_index": tracker.state.activation_absolute_candle_index,
            },
        )
        return


def _maybe_record_wait_started(
    tracker: RecoveryBotTracker,
    *,
    absolute_candle_index: int,
    local_candle_index: int,
    timestamp: str | None,
) -> None:
    if (
        tracker.state.wait_started_recorded
        or not tracker.state.reference_reached
        or tracker.state.recovery_activated
        or tracker.state.activation_absolute_candle_index is None
    ):
        return
    if absolute_candle_index <= int(tracker.state.reference_absolute_candle_index or 0):
        return
    tracker.state.wait_started_recorded = True
    _append_diagnostic(
        tracker,
        purpose="RECOVERY_WAIT_STARTED",
        candle_index=local_candle_index,
        absolute_candle_index=absolute_candle_index,
        timestamp=timestamp,
        metadata={
            "recovery_wait_candles": tracker.config.recovery_wait_candles,
            "recovery_activation_absolute_candle_index": tracker.state.activation_absolute_candle_index,
        },
    )


def _activate_recovery(
    tracker: RecoveryBotTracker,
    sim: "HedgeBotOriginalSimulator",
    *,
    local_candle_index: int,
    absolute_candle_index: int,
    candle: Any,
    cumulative_pnl: float,
) -> None:
    if tracker.config.cancel_open_cycle_orders_on_activation:
        _cancel_open_orders(sim)
    tracker.state.recovery_mode_active = True
    tracker.state.recovery_activated = True
    tracker.state.activation_local_candle_index = local_candle_index
    tracker.state.activation_absolute_candle_index = absolute_candle_index
    tracker.state.activation_timestamp = _iso(getattr(candle, "timestamp", None))

    long_qty = float(sim.book.long_qty)
    short_qty = float(sim.book.short_qty)
    long_avg = float(sim.book.long_avg)
    short_avg = float(sim.book.short_avg)
    reference_price = float(candle.close)
    fee_rate = _resolve_fee_rate(sim)
    lg_cfg = to_long_gap_reduction_config(tracker.config, fee_rate=fee_rate)

    runtime = LongGapReductionRuntime(
        initial_long_qty=long_qty,
        initial_short_qty=short_qty,
        long_avg=long_avg,
        short_avg=short_avg,
        reference_price=reference_price,
        base_main_realized_pnl=float(cumulative_pnl),
        cfg=lg_cfg,
        activation_absolute_candle_index=absolute_candle_index,
    )
    tracker.state.gap_runtime = runtime
    tracker.state.initial_gap_qty = runtime.initial_gap_qty
    runtime.start_event(
        candle,
        local_candle_index=local_candle_index,
        absolute_candle_index=absolute_candle_index,
    )
    _append_diagnostic(
        tracker,
        purpose="RECOVERY_ACTIVATED",
        candle_index=local_candle_index,
        absolute_candle_index=absolute_candle_index,
        timestamp=tracker.state.activation_timestamp,
        metadata={
            "activation_long_qty": long_qty,
            "activation_short_qty": short_qty,
            "activation_gap_qty": runtime.initial_gap_qty,
            "activation_long_avg": long_avg,
            "activation_short_avg": short_avg,
            "short_primary_zero_gap_fail_closed": (
                short_qty > long_qty and runtime.initial_gap_qty <= 0.0
            ),
        },
    )


def _step_purpose_for_index(step_index: int | None) -> str:
    if step_index is None:
        return "RECOVERY_GAP_REDUCE"
    return f"RECOVERY_GAP_REDUCE_STEP_{int(step_index)}"


def _apply_recovery_candle(
    tracker: RecoveryBotTracker,
    sim: "HedgeBotOriginalSimulator",
    *,
    candle: Any,
    local_candle_index: int,
    absolute_candle_index: int,
) -> float:
    runtime = tracker.state.gap_runtime
    if runtime is None:
        return 0.0
    step = runtime.process_candle(
        candle,
        local_candle_index=local_candle_index,
        absolute_candle_index=absolute_candle_index,
    )
    pnl_delta = float(step.gap_reduction_net_pnl) + float(step.joint_exit_net_pnl)
    tracker.state.total_reduced_qty += float(step.reduced_qty)
    tracker.state.recovery_total_gap_reduction_net_pnl += float(step.gap_reduction_net_pnl)

    for event in step.events:
        if event.get("event_type") == "LONG_REDUCE":
            _append_diagnostic(
                tracker,
                purpose=_step_purpose_for_index(event.get("step_index")),
                candle_index=local_candle_index,
                absolute_candle_index=absolute_candle_index,
                timestamp=event.get("timestamp"),
                metadata=dict(event),
            )
        elif event.get("event_type") == "JOINT_EXIT":
            _append_diagnostic(
                tracker,
                purpose="RECOVERY_JOINT_EXIT",
                candle_index=local_candle_index,
                absolute_candle_index=absolute_candle_index,
                timestamp=event.get("timestamp"),
                metadata=dict(event),
            )

    if step.gap_fully_closed:
        tracker.state.gap_fully_closed = True
        tracker.state.remaining_gap_qty = 0.0
        _append_diagnostic(
            tracker,
            purpose="RECOVERY_GAP_CLOSED",
            candle_index=local_candle_index,
            absolute_candle_index=absolute_candle_index,
            timestamp=_iso(getattr(candle, "timestamp", None)),
        )

    if step.recovery_completed:
        tracker.state.recovery_completed = True
        tracker.state.recovery_exit_absolute_candle_index = absolute_candle_index
        tracker.state.recovery_exit_local_candle_index = local_candle_index
        tracker.state.recovery_exit_timestamp = _iso(getattr(candle, "timestamp", None))
        sim.book.long_qty = 0.0
        sim.book.short_qty = 0.0
        sim.book.long_avg = 0.0
        sim.book.short_avg = 0.0
        _cancel_open_orders(sim)

    return pnl_delta


def should_activate_recovery(
    tracker: RecoveryBotTracker,
    *,
    absolute_candle_index: int,
    trade_still_open: bool,
) -> bool:
    if not tracker.state.reference_reached or tracker.state.recovery_activated:
        return False
    if not trade_still_open:
        return False
    activation_index = tracker.state.activation_absolute_candle_index
    if activation_index is None:
        return False
    return absolute_candle_index >= int(activation_index)


def process_recovery_bot_after_normal_candle(
    tracker: RecoveryBotTracker,
    sim: "HedgeBotOriginalSimulator",
    *,
    result: "BacktestResult",
    candle: Any,
    local_candle_index: int,
    absolute_candle_index: int,
    candle_fills: list[FillEvent],
    cumulative_pnl: float,
    trade_still_open: bool,
) -> tuple[float, bool]:
    """
    Run recovery state machine after normal strategy processing on a candle.

    Returns (pnl_delta, force_trade_closed).
    """
    pnl_delta = 0.0
    timestamp = _iso(getattr(candle, "timestamp", None))

    if tracker.state.recovery_mode_active:
        pnl_delta += _apply_recovery_candle(
            tracker,
            sim,
            candle=candle,
            local_candle_index=local_candle_index,
            absolute_candle_index=absolute_candle_index,
        )
        if tracker.state.recovery_completed:
            tracker.state.recovery_final_pnl = float(cumulative_pnl) + float(pnl_delta)
            return pnl_delta, True
        return pnl_delta, False

    _note_reference_from_fills(
        tracker,
        fills=candle_fills,
        local_candle_index=local_candle_index,
        absolute_candle_index=absolute_candle_index,
        timestamp=timestamp,
    )
    _maybe_record_wait_started(
        tracker,
        absolute_candle_index=absolute_candle_index,
        local_candle_index=local_candle_index,
        timestamp=timestamp,
    )

    if trade_still_open and should_activate_recovery(
        tracker,
        absolute_candle_index=absolute_candle_index,
        trade_still_open=True,
    ):
        _activate_recovery(
            tracker,
            sim,
            local_candle_index=local_candle_index,
            absolute_candle_index=absolute_candle_index,
            candle=candle,
            cumulative_pnl=cumulative_pnl,
        )
        pnl_delta += _apply_recovery_candle(
            tracker,
            sim,
            candle=candle,
            local_candle_index=local_candle_index,
            absolute_candle_index=absolute_candle_index,
        )
        if tracker.state.recovery_completed:
            tracker.state.recovery_final_pnl = float(cumulative_pnl) + float(pnl_delta)
            return pnl_delta, True

    return pnl_delta, False


def process_recovery_bot_recovery_only_candle(
    tracker: RecoveryBotTracker,
    sim: "HedgeBotOriginalSimulator",
    *,
    candle: Any,
    local_candle_index: int,
    absolute_candle_index: int,
    cumulative_pnl: float,
) -> tuple[float, bool]:
    """Process a candle when recovery mode is active and normal strategy is skipped."""
    pnl_delta = _apply_recovery_candle(
        tracker,
        sim,
        candle=candle,
        local_candle_index=local_candle_index,
        absolute_candle_index=absolute_candle_index,
    )
    if tracker.state.recovery_completed:
        tracker.state.recovery_final_pnl = float(cumulative_pnl) + float(pnl_delta)
        return pnl_delta, True
    return pnl_delta, False


def populate_recovery_bot_result_fields(
    result: "BacktestResult",
    tracker: RecoveryBotTracker | None,
) -> None:
    if tracker is None:
        result.recovery_bot_enabled = False
        return

    state = tracker.state
    runtime = state.gap_runtime
    result.recovery_bot_enabled = True
    result.recovery_activated = bool(state.recovery_activated)
    result.recovery_reference_purpose = tracker.config.recovery_start_purpose
    result.recovery_reference_absolute_candle_index = state.reference_absolute_candle_index
    result.recovery_reference_timestamp = state.reference_timestamp
    result.recovery_activation_absolute_candle_index = state.activation_absolute_candle_index
    result.recovery_activation_timestamp = state.activation_timestamp
    result.recovery_exit_absolute_candle_index = state.recovery_exit_absolute_candle_index
    result.recovery_exit_timestamp = state.recovery_exit_timestamp
    result.recovery_wait_candles = int(tracker.config.recovery_wait_candles)
    result.recovery_initial_gap_qty = state.initial_gap_qty
    result.recovery_total_reduced_qty = float(state.total_reduced_qty)
    if runtime is not None:
        summary = runtime.summary()
        result.recovery_remaining_gap_qty = summary.get("remaining_gap_qty")
        result.recovery_gap_fully_closed = bool(summary.get("gap_fully_closed"))
        result.recovery_total_gap_reduction_net_pnl = float(
            summary.get("total_gap_reduction_net_pnl") or 0.0
        )
    else:
        result.recovery_remaining_gap_qty = state.remaining_gap_qty
        result.recovery_gap_fully_closed = bool(state.gap_fully_closed)
        result.recovery_total_gap_reduction_net_pnl = float(
            state.recovery_total_gap_reduction_net_pnl
        )
    result.recovery_final_pnl = state.recovery_final_pnl
    if (
        state.recovery_activated
        and state.activation_absolute_candle_index is not None
        and state.recovery_exit_absolute_candle_index is not None
    ):
        result.recovery_duration_candles = int(
            state.recovery_exit_absolute_candle_index - state.activation_absolute_candle_index
        )
        result.recovery_duration_minutes = int(result.recovery_duration_candles) * 5
    result.recovery_diagnostic_events = list(tracker.diagnostic_events)
    if runtime is not None:
        result.recovery_gap_reduction_events = list(runtime.all_events)

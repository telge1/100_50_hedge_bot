from __future__ import annotations

"""Runtime integration for backtest-only long-gap recovery inside historical backtests."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from fixed_cycle_hedge_bot.models import FillEvent

from .long_gap_reduction import LongGapReductionRuntime
from .recovery_bot_config import (
    RecoveryBotConfig,
    normalize_recovery_timeout_action,
    to_long_gap_reduction_config,
)
from .simulated_pnl import closed_pnl_for_virtual_order_fill
from .debug_report import calculate_unrealized_pnl

if TYPE_CHECKING:
    from .backtest_report import BacktestResult
    from .hedge_bot_original_simulator import HedgeBotOriginalSimulator


RECOVERY_DIAGNOSTIC_PURPOSES = (
    "RECOVERY_REFERENCE_REACHED",
    "RECOVERY_WAIT_STARTED",
    "RECOVERY_ACTIVATED",
    "RECOVERY_TIMEOUT_CLOSE_ALL",
    "RECOVERY_MAX_LOSS_CLOSE_ALL",
    "RECOVERY_MAX_ADDITIONAL_LOSS_CLOSE_ALL",
    "RECOVERY_TIMEOUT_CLOSE_SKIPPED",
    "RECOVERY_GAP_REDUCE_STEP_1",
    "RECOVERY_GAP_REDUCE_STEP_2",
    "RECOVERY_GAP_REDUCE_STEP_3",
    "RECOVERY_GAP_REDUCE_STEP_4",
    "RECOVERY_GAP_CLOSED",
    "RECOVERY_JOINT_EXIT",
)

EXIT_REASON_RECOVERY_JOINT = "recovery_joint_exit"
EXIT_REASON_RECOVERY_TIMEOUT_CLOSE = "recovery_timeout_close_all"
EXIT_REASON_RECOVERY_MAX_LOSS_CLOSE = "recovery_max_loss_close_all"
EXIT_REASON_RECOVERY_MAX_ADDITIONAL_LOSS_CLOSE = "recovery_max_additional_loss_close_all"
CLOSE_REASON_TIMEOUT = "timeout"
CLOSE_REASON_MAX_LOSS = "max_loss"
CLOSE_REASON_ADDITIONAL_LOSS = "additional_loss"


@dataclass
class RecoveryBotState:
    reference_reached: bool = False
    reference_absolute_candle_index: int | None = None
    reference_local_candle_index: int | None = None
    reference_timestamp: str | None = None
    activation_absolute_candle_index: int | None = None
    activation_local_candle_index: int | None = None
    activation_timestamp: str | None = None
    planned_timeout_absolute_candle_index: int | None = None
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
    # Timeout close_all state (evaluated once at the wait-end candle only).
    timeout_action_evaluated: bool = False
    timeout_close_triggered: bool = False
    timeout_close_skipped: bool = False
    timeout_close_skip_reason: str | None = None
    timeout_close_net_pnl: float | None = None
    timeout_close_fees: float | None = None
    timeout_close_event: dict[str, Any] | None = None
    timeout_estimated_net_exit_pnl: float | None = None
    # Continuous max-loss stop during wait (checked every candle after reference).
    max_loss_triggered: bool = False
    max_loss_trigger_candle_index: int | None = None
    max_loss_estimated_net_exit_pnl: float | None = None
    # Additional-loss stop relative to reference-fill baseline.
    reference_net_exit_pnl: float | None = None
    current_net_exit_pnl: float | None = None
    additional_loss_usdt: float | None = None
    max_additional_loss_triggered: bool = False
    max_additional_loss_trigger_candle_index: int | None = None
    max_additional_loss_estimated_net_exit_pnl: float | None = None
    close_reason: str | None = None
    gap_reduction_skipped: bool = False
    exit_reason: str | None = None


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


def _estimate_net_exit_pnl_from_sim(
    sim: "HedgeBotOriginalSimulator",
    *,
    candle: Any,
    cumulative_pnl: float,
) -> tuple[float, dict[str, float]]:
    fee_rate = _resolve_fee_rate(sim)
    economics = estimate_timeout_close_economics(
        long_qty=float(sim.book.long_qty),
        short_qty=float(sim.book.short_qty),
        long_avg=float(sim.book.long_avg),
        short_avg=float(sim.book.short_avg),
        execution_price=float(candle.close),
        realized_pnl_before_close=float(cumulative_pnl),
        fee_rate=fee_rate,
    )
    return float(economics["net_pnl_after_close"]), economics


def _note_reference_from_fills(
    tracker: RecoveryBotTracker,
    *,
    fills: list[FillEvent],
    local_candle_index: int,
    absolute_candle_index: int,
    timestamp: str | None,
    sim: "HedgeBotOriginalSimulator | None" = None,
    candle: Any | None = None,
    cumulative_pnl: float | None = None,
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
        tracker.state.planned_timeout_absolute_candle_index = (
            tracker.state.activation_absolute_candle_index
        )
        baseline_meta: dict[str, Any] = {}
        if sim is not None and candle is not None and cumulative_pnl is not None:
            baseline, economics = _estimate_net_exit_pnl_from_sim(
                sim,
                candle=candle,
                cumulative_pnl=float(cumulative_pnl),
            )
            tracker.state.reference_net_exit_pnl = baseline
            tracker.state.current_net_exit_pnl = baseline
            tracker.state.additional_loss_usdt = 0.0
            baseline_meta = {
                "recovery_reference_net_exit_pnl": baseline,
                **{k: economics[k] for k in (
                    "realized_pnl_before_close",
                    "unrealized_long_pnl",
                    "unrealized_short_pnl",
                    "long_closing_fee",
                    "short_closing_fee",
                    "total_closing_fee",
                )},
            }
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
                **baseline_meta,
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
        or tracker.state.timeout_action_evaluated
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
            "recovery_timeout_action": normalize_recovery_timeout_action(
                tracker.config.recovery_timeout_action
            ),
        },
    )


def estimate_timeout_close_economics(
    *,
    long_qty: float,
    short_qty: float,
    long_avg: float,
    short_avg: float,
    execution_price: float,
    realized_pnl_before_close: float,
    fee_rate: float | None,
) -> dict[str, float]:
    """Estimate full flat-close economics using the shared simulated fee model.

    Net after close =
      realized_before
      + long_net_close (gross unrealized long - entry_fee - exit_fee)
      + short_net_close (gross unrealized short - entry_fee - exit_fee)

    Closing fees are charged once via ``closed_pnl_for_virtual_order_fill``.
    """
    unreal_long, unreal_short, combined_unreal = calculate_unrealized_pnl(
        long_qty,
        long_avg,
        short_qty,
        short_avg,
        execution_price,
    )
    unreal_long = float(unreal_long or 0.0)
    unreal_short = float(unreal_short or 0.0)
    combined_unreal = float(combined_unreal or 0.0)

    long_net = 0.0
    short_net = 0.0
    long_entry_fee = 0.0
    long_exit_fee = 0.0
    short_entry_fee = 0.0
    short_exit_fee = 0.0

    if long_qty > 1e-12:
        long_net, long_details = closed_pnl_for_virtual_order_fill(
            side="long",
            reduce_only=True,
            avg_entry_price=float(long_avg),
            fill_price=float(execution_price),
            qty=float(long_qty),
            fee_rate=fee_rate,
        )
        long_entry_fee = float(long_details.get("entry_fee") or 0.0)
        long_exit_fee = float(long_details.get("exit_fee") or 0.0)
    if short_qty > 1e-12:
        short_net, short_details = closed_pnl_for_virtual_order_fill(
            side="short",
            reduce_only=True,
            avg_entry_price=float(short_avg),
            fill_price=float(execution_price),
            qty=float(short_qty),
            fee_rate=fee_rate,
        )
        short_entry_fee = float(short_details.get("entry_fee") or 0.0)
        short_exit_fee = float(short_details.get("exit_fee") or 0.0)

    long_closing_fee = long_entry_fee + long_exit_fee
    short_closing_fee = short_entry_fee + short_exit_fee
    total_closing_fee = long_closing_fee + short_closing_fee
    joint_exit_net = float(long_net) + float(short_net)
    net_after = float(realized_pnl_before_close) + joint_exit_net
    return {
        "realized_pnl_before_close": float(realized_pnl_before_close),
        "unrealized_long_pnl": unreal_long,
        "unrealized_short_pnl": unreal_short,
        "combined_unrealized_pnl": combined_unreal,
        "long_closing_fee": long_closing_fee,
        "short_closing_fee": short_closing_fee,
        "total_closing_fee": total_closing_fee,
        "long_net_close_pnl": float(long_net),
        "short_net_close_pnl": float(short_net),
        "joint_exit_net_pnl": joint_exit_net,
        "net_pnl_after_close": net_after,
    }


def should_execute_timeout_close(
    *,
    estimated_net_pnl_after_close: float,
    min_loss_usdt: float | None,
) -> bool:
    """Return True when close_all should fire at the timeout candle.

    - min_loss_usdt is None: always close
    - otherwise: close only if estimated_net <= -min_loss_usdt
    """
    if min_loss_usdt is None:
        return True
    return float(estimated_net_pnl_after_close) <= -float(min_loss_usdt)


def should_execute_max_loss_close(
    *,
    estimated_net_pnl_after_close: float,
    max_loss_usdt: float,
) -> bool:
    """Return True when continuous max-loss stop should fire during wait."""
    return float(estimated_net_pnl_after_close) <= -float(max_loss_usdt)


def should_execute_additional_loss_close(
    *,
    reference_net_exit_pnl: float,
    current_estimated_net_exit_pnl: float,
    max_additional_loss_usdt: float,
) -> bool:
    """Return True when additional loss since reference baseline reaches the limit."""
    additional_loss = float(reference_net_exit_pnl) - float(current_estimated_net_exit_pnl)
    return additional_loss >= float(max_additional_loss_usdt)


def _execute_flat_close_all(
    tracker: RecoveryBotTracker,
    sim: "HedgeBotOriginalSimulator",
    *,
    local_candle_index: int,
    absolute_candle_index: int,
    candle: Any,
    cumulative_pnl: float,
    close_reason: str,
) -> float:
    """Fully close both legs at candle.close and mark the trade flat.

    ``close_reason`` is ``timeout``, ``max_loss``, or ``additional_loss``.
    Returns the joint-exit net PnL delta (fees included once).
    """
    allowed = {
        CLOSE_REASON_TIMEOUT,
        CLOSE_REASON_MAX_LOSS,
        CLOSE_REASON_ADDITIONAL_LOSS,
    }
    if close_reason not in allowed:
        raise ValueError(f"unsupported close_reason={close_reason!r}")

    if tracker.config.cancel_open_cycle_orders_on_activation:
        _cancel_open_orders(sim)

    long_qty = float(sim.book.long_qty)
    short_qty = float(sim.book.short_qty)
    long_avg = float(sim.book.long_avg)
    short_avg = float(sim.book.short_avg)
    execution_price = float(candle.close)
    fee_rate = _resolve_fee_rate(sim)
    economics = estimate_timeout_close_economics(
        long_qty=long_qty,
        short_qty=short_qty,
        long_avg=long_avg,
        short_avg=short_avg,
        execution_price=execution_price,
        realized_pnl_before_close=float(cumulative_pnl),
        fee_rate=fee_rate,
    )
    estimated = float(economics["net_pnl_after_close"])
    reference_baseline = tracker.state.reference_net_exit_pnl
    additional_loss = None
    if reference_baseline is not None:
        additional_loss = float(reference_baseline) - estimated

    if close_reason == CLOSE_REASON_MAX_LOSS:
        event_type = "RECOVERY_MAX_LOSS_CLOSE_ALL"
        exit_reason = EXIT_REASON_RECOVERY_MAX_LOSS_CLOSE
        diagnostic_purpose = "RECOVERY_MAX_LOSS_CLOSE_ALL"
    elif close_reason == CLOSE_REASON_ADDITIONAL_LOSS:
        event_type = "RECOVERY_MAX_ADDITIONAL_LOSS_CLOSE_ALL"
        exit_reason = EXIT_REASON_RECOVERY_MAX_ADDITIONAL_LOSS_CLOSE
        diagnostic_purpose = "RECOVERY_MAX_ADDITIONAL_LOSS_CLOSE_ALL"
    else:
        event_type = "RECOVERY_TIMEOUT_CLOSE_ALL"
        exit_reason = EXIT_REASON_RECOVERY_TIMEOUT_CLOSE
        diagnostic_purpose = "RECOVERY_TIMEOUT_CLOSE_ALL"

    event = {
        "event_type": event_type,
        "close_reason": close_reason,
        "timestamp": _iso(getattr(candle, "timestamp", None)),
        "local_candle_index": local_candle_index,
        "absolute_candle_index": absolute_candle_index,
        "reference_fill_purpose": tracker.config.recovery_start_purpose,
        "reference_fill_index": tracker.state.reference_absolute_candle_index,
        "wait_candles": int(tracker.config.recovery_wait_candles),
        "max_loss_usdt": tracker.config.recovery_max_loss_usdt,
        "max_additional_loss_usdt": tracker.config.recovery_max_additional_loss_usdt,
        "reference_net_exit_pnl": reference_baseline,
        "additional_loss_usdt": additional_loss,
        "long_qty_before": long_qty,
        "short_qty_before": short_qty,
        "long_avg": long_avg,
        "short_avg": short_avg,
        "execution_price": execution_price,
        "fee_rate": fee_rate,
        **economics,
    }

    tracker.state.close_reason = close_reason
    tracker.state.timeout_action_evaluated = True
    tracker.state.timeout_close_event = event
    tracker.state.timeout_close_net_pnl = estimated
    tracker.state.timeout_close_fees = float(economics["total_closing_fee"])
    tracker.state.timeout_estimated_net_exit_pnl = estimated
    tracker.state.current_net_exit_pnl = estimated
    if additional_loss is not None:
        tracker.state.additional_loss_usdt = additional_loss
    tracker.state.gap_reduction_skipped = True
    tracker.state.recovery_activated = True
    tracker.state.recovery_mode_active = False
    tracker.state.recovery_completed = True
    tracker.state.activation_local_candle_index = local_candle_index
    tracker.state.activation_absolute_candle_index = absolute_candle_index
    tracker.state.activation_timestamp = event["timestamp"]
    tracker.state.recovery_exit_local_candle_index = local_candle_index
    tracker.state.recovery_exit_absolute_candle_index = absolute_candle_index
    tracker.state.recovery_exit_timestamp = event["timestamp"]
    tracker.state.initial_gap_qty = max(long_qty - short_qty, 0.0)
    tracker.state.remaining_gap_qty = 0.0
    tracker.state.gap_fully_closed = True
    tracker.state.exit_reason = exit_reason
    tracker.state.recovery_final_pnl = estimated

    tracker.state.timeout_close_triggered = False
    tracker.state.max_loss_triggered = False
    tracker.state.max_additional_loss_triggered = False
    if close_reason == CLOSE_REASON_MAX_LOSS:
        tracker.state.max_loss_triggered = True
        tracker.state.max_loss_trigger_candle_index = absolute_candle_index
        tracker.state.max_loss_estimated_net_exit_pnl = estimated
    elif close_reason == CLOSE_REASON_ADDITIONAL_LOSS:
        tracker.state.max_additional_loss_triggered = True
        tracker.state.max_additional_loss_trigger_candle_index = absolute_candle_index
        tracker.state.max_additional_loss_estimated_net_exit_pnl = estimated
    else:
        tracker.state.timeout_close_triggered = True

    sim.book.long_qty = 0.0
    sim.book.short_qty = 0.0
    sim.book.long_avg = 0.0
    sim.book.short_avg = 0.0
    _cancel_open_orders(sim)

    _append_diagnostic(
        tracker,
        purpose=diagnostic_purpose,
        candle_index=local_candle_index,
        absolute_candle_index=absolute_candle_index,
        timestamp=event["timestamp"],
        metadata=dict(event),
    )
    return float(economics["joint_exit_net_pnl"])


def _execute_timeout_close_all(
    tracker: RecoveryBotTracker,
    sim: "HedgeBotOriginalSimulator",
    *,
    local_candle_index: int,
    absolute_candle_index: int,
    candle: Any,
    cumulative_pnl: float,
) -> float:
    """Fully close both legs at wait-end timeout (legacy wrapper)."""
    return _execute_flat_close_all(
        tracker,
        sim,
        local_candle_index=local_candle_index,
        absolute_candle_index=absolute_candle_index,
        candle=candle,
        cumulative_pnl=cumulative_pnl,
        close_reason=CLOSE_REASON_TIMEOUT,
    )


def _maybe_execute_wait_phase_loss_closes(
    tracker: RecoveryBotTracker,
    sim: "HedgeBotOriginalSimulator",
    *,
    local_candle_index: int,
    absolute_candle_index: int,
    candle: Any,
    cumulative_pnl: float,
    trade_still_open: bool,
) -> tuple[float, bool]:
    """Check absolute max-loss and additional-loss stops after the reference fill.

    First matching condition wins. Returns (pnl_delta, force_trade_closed).
    """
    max_loss = tracker.config.recovery_max_loss_usdt
    max_additional = tracker.config.recovery_max_additional_loss_usdt
    if (
        (max_loss is None and max_additional is None)
        or not trade_still_open
        or tracker.state.recovery_completed
        or tracker.state.timeout_action_evaluated
        or tracker.state.recovery_mode_active
        or not tracker.state.reference_reached
        or tracker.state.reference_absolute_candle_index is None
    ):
        return 0.0, False
    if absolute_candle_index <= int(tracker.state.reference_absolute_candle_index):
        return 0.0, False

    estimated, _economics = _estimate_net_exit_pnl_from_sim(
        sim,
        candle=candle,
        cumulative_pnl=cumulative_pnl,
    )
    tracker.state.current_net_exit_pnl = estimated
    baseline = tracker.state.reference_net_exit_pnl
    if baseline is not None:
        tracker.state.additional_loss_usdt = float(baseline) - estimated

    if max_loss is not None and should_execute_max_loss_close(
        estimated_net_pnl_after_close=estimated,
        max_loss_usdt=float(max_loss),
    ):
        pnl_delta = _execute_flat_close_all(
            tracker,
            sim,
            local_candle_index=local_candle_index,
            absolute_candle_index=absolute_candle_index,
            candle=candle,
            cumulative_pnl=cumulative_pnl,
            close_reason=CLOSE_REASON_MAX_LOSS,
        )
        return pnl_delta, True

    if (
        max_additional is not None
        and baseline is not None
        and should_execute_additional_loss_close(
            reference_net_exit_pnl=float(baseline),
            current_estimated_net_exit_pnl=estimated,
            max_additional_loss_usdt=float(max_additional),
        )
    ):
        pnl_delta = _execute_flat_close_all(
            tracker,
            sim,
            local_candle_index=local_candle_index,
            absolute_candle_index=absolute_candle_index,
            candle=candle,
            cumulative_pnl=cumulative_pnl,
            close_reason=CLOSE_REASON_ADDITIONAL_LOSS,
        )
        return pnl_delta, True

    return 0.0, False


def _maybe_execute_max_loss_close(
    tracker: RecoveryBotTracker,
    sim: "HedgeBotOriginalSimulator",
    *,
    local_candle_index: int,
    absolute_candle_index: int,
    candle: Any,
    cumulative_pnl: float,
    trade_still_open: bool,
) -> tuple[float, bool]:
    """Backward-compatible alias for wait-phase loss closes."""
    return _maybe_execute_wait_phase_loss_closes(
        tracker,
        sim,
        local_candle_index=local_candle_index,
        absolute_candle_index=absolute_candle_index,
        candle=candle,
        cumulative_pnl=cumulative_pnl,
        trade_still_open=trade_still_open,
    )


def _skip_timeout_close(
    tracker: RecoveryBotTracker,
    *,
    local_candle_index: int,
    absolute_candle_index: int,
    candle: Any,
    economics: dict[str, float],
    reason: str,
) -> None:
    tracker.state.timeout_close_skipped = True
    tracker.state.timeout_close_skip_reason = reason
    tracker.state.gap_reduction_skipped = True
    tracker.state.timeout_estimated_net_exit_pnl = float(
        economics.get("net_pnl_after_close") or 0.0
    )
    _append_diagnostic(
        tracker,
        purpose="RECOVERY_TIMEOUT_CLOSE_SKIPPED",
        candle_index=local_candle_index,
        absolute_candle_index=absolute_candle_index,
        timestamp=_iso(getattr(candle, "timestamp", None)),
        metadata={
            "reason": reason,
            "min_loss_usdt": tracker.config.recovery_timeout_min_loss_usdt,
            "estimated_net_pnl_after_close": economics.get("net_pnl_after_close"),
            "reevaluate_later": False,
            "note": (
                "Timeout close is evaluated once at the wait-end candle only; "
                "if the loss gate is not met the trade continues without recovery."
            ),
            **economics,
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
        tracker.state.exit_reason = tracker.state.exit_reason or EXIT_REASON_RECOVERY_JOINT
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
    if tracker.state.timeout_action_evaluated:
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
            tracker.state.exit_reason = tracker.state.exit_reason or EXIT_REASON_RECOVERY_JOINT
            return pnl_delta, True
        return pnl_delta, False

    _note_reference_from_fills(
        tracker,
        fills=candle_fills,
        local_candle_index=local_candle_index,
        absolute_candle_index=absolute_candle_index,
        timestamp=timestamp,
        sim=sim,
        candle=candle,
        cumulative_pnl=cumulative_pnl,
    )
    _maybe_record_wait_started(
        tracker,
        absolute_candle_index=absolute_candle_index,
        local_candle_index=local_candle_index,
        timestamp=timestamp,
    )

    max_loss_pnl, max_loss_closed = _maybe_execute_wait_phase_loss_closes(
        tracker,
        sim,
        local_candle_index=local_candle_index,
        absolute_candle_index=absolute_candle_index,
        candle=candle,
        cumulative_pnl=cumulative_pnl,
        trade_still_open=trade_still_open,
    )
    if max_loss_closed:
        return max_loss_pnl, True
    pnl_delta += max_loss_pnl

    if trade_still_open and should_activate_recovery(
        tracker,
        absolute_candle_index=absolute_candle_index,
        trade_still_open=True,
    ):
        timeout_action = normalize_recovery_timeout_action(
            tracker.config.recovery_timeout_action
        )
        if timeout_action == "close_all":
            tracker.state.timeout_action_evaluated = True
            fee_rate = _resolve_fee_rate(sim)
            economics = estimate_timeout_close_economics(
                long_qty=float(sim.book.long_qty),
                short_qty=float(sim.book.short_qty),
                long_avg=float(sim.book.long_avg),
                short_avg=float(sim.book.short_avg),
                execution_price=float(candle.close),
                realized_pnl_before_close=float(cumulative_pnl),
                fee_rate=fee_rate,
            )
            if should_execute_timeout_close(
                estimated_net_pnl_after_close=float(economics["net_pnl_after_close"]),
                min_loss_usdt=tracker.config.recovery_timeout_min_loss_usdt,
            ):
                pnl_delta += _execute_timeout_close_all(
                    tracker,
                    sim,
                    local_candle_index=local_candle_index,
                    absolute_candle_index=absolute_candle_index,
                    candle=candle,
                    cumulative_pnl=cumulative_pnl,
                )
                return pnl_delta, True
            _skip_timeout_close(
                tracker,
                local_candle_index=local_candle_index,
                absolute_candle_index=absolute_candle_index,
                candle=candle,
                economics=economics,
                reason="min_loss_not_met",
            )
            return pnl_delta, False

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
            tracker.state.exit_reason = EXIT_REASON_RECOVERY_JOINT
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
        tracker.state.exit_reason = tracker.state.exit_reason or EXIT_REASON_RECOVERY_JOINT
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
    result.recovery_timeout_action = normalize_recovery_timeout_action(
        tracker.config.recovery_timeout_action
    )
    result.recovery_timeout_min_loss_usdt = tracker.config.recovery_timeout_min_loss_usdt
    result.recovery_timeout_close_triggered = bool(state.timeout_close_triggered)
    result.recovery_timeout_close_index = (
        state.recovery_exit_absolute_candle_index if state.timeout_close_triggered else None
    )
    result.recovery_timeout_close_net_pnl = state.timeout_close_net_pnl
    result.recovery_timeout_close_fees = state.timeout_close_fees
    result.recovery_gap_reduction_skipped = bool(state.gap_reduction_skipped)
    result.recovery_timeout_close_event = (
        dict(state.timeout_close_event) if state.timeout_close_event else None
    )
    result.recovery_reference_fill_candle_index = state.reference_absolute_candle_index
    result.recovery_timeout_target_candle_index = (
        state.planned_timeout_absolute_candle_index or state.activation_absolute_candle_index
    )
    result.recovery_timeout_triggered = bool(state.timeout_close_triggered)
    result.recovery_timeout_trigger_candle_index = (
        state.recovery_exit_absolute_candle_index if state.timeout_close_triggered else None
    )
    result.recovery_timeout_skip_reason = state.timeout_close_skip_reason
    result.recovery_timeout_estimated_net_exit_pnl = state.timeout_estimated_net_exit_pnl
    result.recovery_max_loss_usdt = tracker.config.recovery_max_loss_usdt
    result.recovery_max_loss_triggered = bool(state.max_loss_triggered)
    result.recovery_max_loss_trigger_candle_index = state.max_loss_trigger_candle_index
    result.recovery_max_loss_estimated_net_exit_pnl = state.max_loss_estimated_net_exit_pnl
    result.recovery_max_additional_loss_usdt = tracker.config.recovery_max_additional_loss_usdt
    result.recovery_reference_net_exit_pnl = state.reference_net_exit_pnl
    result.recovery_current_net_exit_pnl = state.current_net_exit_pnl
    result.recovery_additional_loss_usdt = state.additional_loss_usdt
    result.recovery_max_additional_loss_triggered = bool(state.max_additional_loss_triggered)
    result.recovery_max_additional_loss_trigger_candle_index = (
        state.max_additional_loss_trigger_candle_index
    )
    result.recovery_max_additional_loss_estimated_net_exit_pnl = (
        state.max_additional_loss_estimated_net_exit_pnl
    )
    result.recovery_close_reason = state.close_reason
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
    elif state.timeout_close_event is not None:
        result.recovery_gap_reduction_events = [dict(state.timeout_close_event)]

"""Evaluate delayed recovery activation after a reference fill."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


CANDLE_INTERVAL_MINUTES = 5


@dataclass(frozen=True)
class TradeFillReplayRow:
    absolute_candle_index: int
    timestamp: str | None
    purpose: str
    fill_price: float | None
    long_qty_after: float
    short_qty_after: float
    long_avg_after: float
    short_avg_after: float
    cumulative_realized_pnl_net: float
    cycle_index: int | None
    flat_after_fill: bool


@dataclass(frozen=True)
class RecoveryWaitEvaluation:
    recovery_reference_purpose: str
    recovery_reference_timestamp: str | None
    recovery_reference_absolute_candle_index: int | None
    recovery_wait_candles: int
    recovery_wait_minutes: int
    recovery_activation_absolute_candle_index: int | None
    recovery_activation_timestamp: str | None
    original_exit_timestamp: str | None
    original_exit_absolute_candle_index: int | None
    original_closed_before_recovery: bool
    original_exit_timing: str | None
    recovery_activated: bool
    activation_reason: str | None
    non_activation_reason: str | None
    activation_long_qty: float | None
    activation_short_qty: float | None
    activation_gap_qty: float | None
    activation_long_avg: float | None
    activation_short_avg: float | None
    activation_base_main_realized_pnl: float | None
    activation_active_cycle_index: int | None
    activation_reference_price: float | None
    activation_state_source: str | None
    eligible_for_simulation: bool
    diagnostics_errors: tuple[str, ...]


def _parse_iso_timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _format_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def fill_row_absolute_candle_index(
    row: dict[str, Any],
    *,
    run_start_index: int,
    input_slice_start_index: int,
) -> int | None:
    global_idx = row.get("absolute_candle_index")
    if global_idx is not None:
        try:
            return int(input_slice_start_index) + int(global_idx)
        except (TypeError, ValueError):
            return None
    slice_global = row.get("global_candle_index")
    if slice_global is not None:
        try:
            return int(input_slice_start_index) + int(slice_global)
        except (TypeError, ValueError):
            return None
    local_idx = row.get("local_candle_index", row.get("candle_index"))
    if local_idx is None:
        return None
    try:
        return int(input_slice_start_index) + int(run_start_index) + int(local_idx)
    except (TypeError, ValueError):
        return None


def load_trade_fill_replay_rows(
    trade_blocks_path: Path,
    *,
    run_start_index: int,
    input_slice_start_index: int,
) -> list[TradeFillReplayRow]:
    payload = json.loads(trade_blocks_path.read_text(encoding="utf-8"))
    blocks = list(payload.get("trade_blocks") or payload.get("rows") or [])
    rows: list[TradeFillReplayRow] = []
    for block in blocks:
        if str(block.get("row_type") or "") != "fill":
            continue
        absolute_index = fill_row_absolute_candle_index(
            block,
            run_start_index=run_start_index,
            input_slice_start_index=input_slice_start_index,
        )
        if absolute_index is None:
            continue
        long_qty = block.get("long_qty_after")
        short_qty = block.get("short_qty_after")
        long_avg = block.get("long_avg_after")
        short_avg = block.get("short_avg_after")
        fill_price = block.get("fill_price")
        cumulative = block.get("cumulative_realized_pnl_net")
        if cumulative is None:
            cumulative = block.get("cumulative_pnl")
        if cumulative is None:
            cumulative = block.get("confirmed_closed_pnl")
        if any(value is None for value in (long_qty, short_qty, long_avg, short_avg)):
            continue
        try:
            rows.append(
                TradeFillReplayRow(
                    absolute_candle_index=int(absolute_index),
                    timestamp=str(block.get("timestamp") or "") or None,
                    purpose=str(block.get("purpose") or ""),
                    fill_price=float(fill_price) if fill_price is not None else None,
                    long_qty_after=float(long_qty),
                    short_qty_after=float(short_qty),
                    long_avg_after=float(long_avg),
                    short_avg_after=float(short_avg),
                    cumulative_realized_pnl_net=float(cumulative or 0.0),
                    cycle_index=int(block["cycle_index"]) if block.get("cycle_index") is not None else None,
                    flat_after_fill=float(long_qty) <= 0.0 and float(short_qty) <= 0.0,
                )
            )
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda item: (item.absolute_candle_index, item.timestamp or ""))
    return rows


def replay_state_at_absolute_index(
    fills: list[TradeFillReplayRow],
    activation_absolute_index: int,
) -> TradeFillReplayRow | None:
    state: TradeFillReplayRow | None = None
    for fill in fills:
        if fill.absolute_candle_index > activation_absolute_index:
            break
        state = fill
    return state


def original_exit_absolute_index(
    run: dict[str, Any],
    *,
    input_slice_start_index: int,
) -> int | None:
    """Return the candle-file index where the original trade closed."""
    if str(run.get("final_status") or "").lower() != "closed":
        return None
    end_index = run.get("end_index")
    if end_index is None:
        return None
    try:
        return int(input_slice_start_index) + int(end_index)
    except (TypeError, ValueError):
        return None


def classify_original_exit_timing(
    *,
    original_exit_absolute_candle_index: int | None,
    activation_absolute_candle_index: int | None,
) -> str | None:
    if original_exit_absolute_candle_index is None or activation_absolute_candle_index is None:
        return None
    if original_exit_absolute_candle_index < activation_absolute_candle_index:
        return "before_wait_end"
    if original_exit_absolute_candle_index == activation_absolute_candle_index:
        return "same_candle_as_activation"
    return "after_wait_end"


def evaluate_recovery_wait_activation(
    *,
    run: dict[str, Any],
    reference_snapshot: dict[str, Any],
    trade_blocks_path: Path | None,
    candles: list[Any],
    input_slice_start_index: int,
    recovery_start_purpose: str,
    recovery_wait_candles: int,
    series_last_absolute_index: int,
) -> RecoveryWaitEvaluation:
    wait_candles = max(0, int(recovery_wait_candles))
    wait_minutes = wait_candles * CANDLE_INTERVAL_MINUTES
    trade_number = int(run.get("trade_number") or 0)
    run_start_index = int(run.get("start_index") or 0)
    diagnostics: list[str] = []

    reference_absolute = int(
        reference_snapshot.get("recovery_candle_index")
        or reference_snapshot["cycle3_candle_index"]
    )
    reference_timestamp = (
        reference_snapshot.get("recovery_fill_timestamp")
        or reference_snapshot.get("cycle3_fill_timestamp")
    )
    activation_absolute = reference_absolute + wait_candles
    if activation_absolute < 0 or activation_absolute >= len(candles):
        return RecoveryWaitEvaluation(
            recovery_reference_purpose=recovery_start_purpose,
            recovery_reference_timestamp=reference_timestamp,
            recovery_reference_absolute_candle_index=reference_absolute,
            recovery_wait_candles=wait_candles,
            recovery_wait_minutes=wait_minutes,
            recovery_activation_absolute_candle_index=activation_absolute,
            recovery_activation_timestamp=None,
            original_exit_timestamp=run.get("end_time"),
            original_exit_absolute_candle_index=original_exit_absolute_index(
                run,
                input_slice_start_index=input_slice_start_index,
            ),
            original_closed_before_recovery=False,
            original_exit_timing=None,
            recovery_activated=False,
            activation_reason=None,
            non_activation_reason="series_ended_before_activation",
            activation_long_qty=None,
            activation_short_qty=None,
            activation_gap_qty=None,
            activation_long_avg=None,
            activation_short_avg=None,
            activation_base_main_realized_pnl=None,
            activation_active_cycle_index=None,
            activation_reference_price=None,
            activation_state_source=None,
            eligible_for_simulation=False,
            diagnostics_errors=tuple(diagnostics),
        )

    activation_candle = candles[activation_absolute]
    activation_timestamp = _format_timestamp(getattr(activation_candle, "timestamp", None))

    original_exit_abs = original_exit_absolute_index(
        run,
        input_slice_start_index=input_slice_start_index,
    )
    original_exit_ts = run.get("end_time") if original_exit_abs is not None else None
    exit_timing = classify_original_exit_timing(
        original_exit_absolute_candle_index=original_exit_abs,
        activation_absolute_candle_index=activation_absolute,
    )
    original_closed_before = (
        original_exit_abs is not None and original_exit_abs <= activation_absolute
    )

    if trade_blocks_path is None or not trade_blocks_path.exists():
        return RecoveryWaitEvaluation(
            recovery_reference_purpose=recovery_start_purpose,
            recovery_reference_timestamp=reference_timestamp,
            recovery_reference_absolute_candle_index=reference_absolute,
            recovery_wait_candles=wait_candles,
            recovery_wait_minutes=wait_minutes,
            recovery_activation_absolute_candle_index=activation_absolute,
            recovery_activation_timestamp=activation_timestamp,
            original_exit_timestamp=original_exit_ts,
            original_exit_absolute_candle_index=original_exit_abs,
            original_closed_before_recovery=original_closed_before,
            original_exit_timing=exit_timing,
            recovery_activated=False,
            activation_reason=None,
            non_activation_reason="activation_state_unavailable",
            activation_long_qty=None,
            activation_short_qty=None,
            activation_gap_qty=None,
            activation_long_avg=None,
            activation_short_avg=None,
            activation_base_main_realized_pnl=None,
            activation_active_cycle_index=None,
            activation_reference_price=None,
            activation_state_source=None,
            eligible_for_simulation=False,
            diagnostics_errors=("trade_blocks_file_not_found",),
        )

    fills = load_trade_fill_replay_rows(
        trade_blocks_path,
        run_start_index=run_start_index,
        input_slice_start_index=input_slice_start_index,
    )
    if not fills:
        return RecoveryWaitEvaluation(
            recovery_reference_purpose=recovery_start_purpose,
            recovery_reference_timestamp=reference_timestamp,
            recovery_reference_absolute_candle_index=reference_absolute,
            recovery_wait_candles=wait_candles,
            recovery_wait_minutes=wait_minutes,
            recovery_activation_absolute_candle_index=activation_absolute,
            recovery_activation_timestamp=activation_timestamp,
            original_exit_timestamp=original_exit_ts,
            original_exit_absolute_candle_index=original_exit_abs,
            original_closed_before_recovery=original_closed_before,
            original_exit_timing=exit_timing,
            recovery_activated=False,
            activation_reason=None,
            non_activation_reason="activation_state_unavailable",
            activation_long_qty=None,
            activation_short_qty=None,
            activation_gap_qty=None,
            activation_long_avg=None,
            activation_short_avg=None,
            activation_base_main_realized_pnl=None,
            activation_active_cycle_index=None,
            activation_reference_price=None,
            activation_state_source=None,
            eligible_for_simulation=False,
            diagnostics_errors=("no_fill_rows_for_replay",),
        )

    if original_closed_before:
        return RecoveryWaitEvaluation(
            recovery_reference_purpose=recovery_start_purpose,
            recovery_reference_timestamp=reference_timestamp,
            recovery_reference_absolute_candle_index=reference_absolute,
            recovery_wait_candles=wait_candles,
            recovery_wait_minutes=wait_minutes,
            recovery_activation_absolute_candle_index=activation_absolute,
            recovery_activation_timestamp=activation_timestamp,
            original_exit_timestamp=original_exit_ts,
            original_exit_absolute_candle_index=original_exit_abs,
            original_closed_before_recovery=True,
            original_exit_timing=exit_timing,
            recovery_activated=False,
            activation_reason=None,
            non_activation_reason="original_trade_closed_before_recovery",
            activation_long_qty=None,
            activation_short_qty=None,
            activation_gap_qty=None,
            activation_long_avg=None,
            activation_short_avg=None,
            activation_base_main_realized_pnl=None,
            activation_active_cycle_index=None,
            activation_reference_price=None,
            activation_state_source="trade_blocks_fill_replay",
            eligible_for_simulation=False,
            diagnostics_errors=tuple(diagnostics),
        )

    if activation_absolute > series_last_absolute_index:
        return RecoveryWaitEvaluation(
            recovery_reference_purpose=recovery_start_purpose,
            recovery_reference_timestamp=reference_timestamp,
            recovery_reference_absolute_candle_index=reference_absolute,
            recovery_wait_candles=wait_candles,
            recovery_wait_minutes=wait_minutes,
            recovery_activation_absolute_candle_index=activation_absolute,
            recovery_activation_timestamp=activation_timestamp,
            original_exit_timestamp=original_exit_ts,
            original_exit_absolute_candle_index=original_exit_abs,
            original_closed_before_recovery=False,
            original_exit_timing=exit_timing,
            recovery_activated=False,
            activation_reason=None,
            non_activation_reason="series_ended_before_activation",
            activation_long_qty=None,
            activation_short_qty=None,
            activation_gap_qty=None,
            activation_long_avg=None,
            activation_short_avg=None,
            activation_base_main_realized_pnl=None,
            activation_active_cycle_index=None,
            activation_reference_price=None,
            activation_state_source="trade_blocks_fill_replay",
            eligible_for_simulation=False,
            diagnostics_errors=tuple(diagnostics),
        )

    replay_state = replay_state_at_absolute_index(fills, activation_absolute)
    if replay_state is None:
        return RecoveryWaitEvaluation(
            recovery_reference_purpose=recovery_start_purpose,
            recovery_reference_timestamp=reference_timestamp,
            recovery_reference_absolute_candle_index=reference_absolute,
            recovery_wait_candles=wait_candles,
            recovery_wait_minutes=wait_minutes,
            recovery_activation_absolute_candle_index=activation_absolute,
            recovery_activation_timestamp=activation_timestamp,
            original_exit_timestamp=original_exit_ts,
            original_exit_absolute_candle_index=original_exit_abs,
            original_closed_before_recovery=False,
            original_exit_timing=exit_timing,
            recovery_activated=False,
            activation_reason=None,
            non_activation_reason="activation_state_unavailable",
            activation_long_qty=None,
            activation_short_qty=None,
            activation_gap_qty=None,
            activation_long_avg=None,
            activation_short_avg=None,
            activation_base_main_realized_pnl=None,
            activation_active_cycle_index=None,
            activation_reference_price=None,
            activation_state_source="trade_blocks_fill_replay",
            eligible_for_simulation=False,
            diagnostics_errors=("activation_state_not_found",),
        )

    if replay_state.flat_after_fill:
        return RecoveryWaitEvaluation(
            recovery_reference_purpose=recovery_start_purpose,
            recovery_reference_timestamp=reference_timestamp,
            recovery_reference_absolute_candle_index=reference_absolute,
            recovery_wait_candles=wait_candles,
            recovery_wait_minutes=wait_minutes,
            recovery_activation_absolute_candle_index=activation_absolute,
            recovery_activation_timestamp=activation_timestamp,
            original_exit_timestamp=original_exit_ts,
            original_exit_absolute_candle_index=original_exit_abs,
            original_closed_before_recovery=True,
            original_exit_timing=exit_timing or "same_candle_as_activation",
            recovery_activated=False,
            activation_reason=None,
            non_activation_reason="original_trade_closed_before_recovery",
            activation_long_qty=replay_state.long_qty_after,
            activation_short_qty=replay_state.short_qty_after,
            activation_gap_qty=0.0,
            activation_long_avg=replay_state.long_avg_after,
            activation_short_avg=replay_state.short_avg_after,
            activation_base_main_realized_pnl=replay_state.cumulative_realized_pnl_net,
            activation_active_cycle_index=replay_state.cycle_index,
            activation_reference_price=replay_state.fill_price,
            activation_state_source="trade_blocks_fill_replay",
            eligible_for_simulation=False,
            diagnostics_errors=tuple(diagnostics),
        )

    activation_gap = max(replay_state.long_qty_after - replay_state.short_qty_after, 0.0)
    if activation_gap <= 0.0:
        return RecoveryWaitEvaluation(
            recovery_reference_purpose=recovery_start_purpose,
            recovery_reference_timestamp=reference_timestamp,
            recovery_reference_absolute_candle_index=reference_absolute,
            recovery_wait_candles=wait_candles,
            recovery_wait_minutes=wait_minutes,
            recovery_activation_absolute_candle_index=activation_absolute,
            recovery_activation_timestamp=activation_timestamp,
            original_exit_timestamp=original_exit_ts,
            original_exit_absolute_candle_index=original_exit_abs,
            original_closed_before_recovery=False,
            original_exit_timing=exit_timing,
            recovery_activated=False,
            activation_reason=None,
            non_activation_reason="no_long_short_gap_at_activation",
            activation_long_qty=replay_state.long_qty_after,
            activation_short_qty=replay_state.short_qty_after,
            activation_gap_qty=activation_gap,
            activation_long_avg=replay_state.long_avg_after,
            activation_short_avg=replay_state.short_avg_after,
            activation_base_main_realized_pnl=replay_state.cumulative_realized_pnl_net,
            activation_active_cycle_index=replay_state.cycle_index,
            activation_reference_price=float(getattr(activation_candle, "close", replay_state.fill_price or 0.0)),
            activation_state_source="trade_blocks_fill_replay",
            eligible_for_simulation=False,
            diagnostics_errors=tuple(diagnostics),
        )

    reference_price = float(getattr(activation_candle, "close", replay_state.fill_price or 0.0))
    if replay_state.absolute_candle_index == activation_absolute and replay_state.fill_price is not None:
        reference_price = float(replay_state.fill_price)

    return RecoveryWaitEvaluation(
        recovery_reference_purpose=recovery_start_purpose,
        recovery_reference_timestamp=reference_timestamp,
        recovery_reference_absolute_candle_index=reference_absolute,
        recovery_wait_candles=wait_candles,
        recovery_wait_minutes=wait_minutes,
        recovery_activation_absolute_candle_index=activation_absolute,
        recovery_activation_timestamp=activation_timestamp,
        original_exit_timestamp=original_exit_ts,
        original_exit_absolute_candle_index=original_exit_abs,
        original_closed_before_recovery=False,
        original_exit_timing=exit_timing,
        recovery_activated=True,
        activation_reason="wait_elapsed_trade_still_open",
        non_activation_reason=None,
        activation_long_qty=replay_state.long_qty_after,
        activation_short_qty=replay_state.short_qty_after,
        activation_gap_qty=activation_gap,
        activation_long_avg=replay_state.long_avg_after,
        activation_short_avg=replay_state.short_avg_after,
        activation_base_main_realized_pnl=replay_state.cumulative_realized_pnl_net,
        activation_active_cycle_index=replay_state.cycle_index,
        activation_reference_price=reference_price,
        activation_state_source="trade_blocks_fill_replay",
        eligible_for_simulation=True,
        diagnostics_errors=tuple(diagnostics),
    )


def evaluation_to_dict(evaluation: RecoveryWaitEvaluation) -> dict[str, Any]:
    return {
        "recovery_reference_purpose": evaluation.recovery_reference_purpose,
        "recovery_reference_timestamp": evaluation.recovery_reference_timestamp,
        "recovery_reference_absolute_candle_index": evaluation.recovery_reference_absolute_candle_index,
        "recovery_wait_candles": evaluation.recovery_wait_candles,
        "recovery_wait_minutes": evaluation.recovery_wait_minutes,
        "recovery_activation_timestamp": evaluation.recovery_activation_timestamp,
        "recovery_activation_absolute_candle_index": evaluation.recovery_activation_absolute_candle_index,
        "original_exit_timestamp": evaluation.original_exit_timestamp,
        "original_exit_absolute_candle_index": evaluation.original_exit_absolute_candle_index,
        "original_closed_before_recovery": evaluation.original_closed_before_recovery,
        "original_exit_timing": evaluation.original_exit_timing,
        "recovery_activated": evaluation.recovery_activated,
        "activation_reason": evaluation.activation_reason,
        "non_activation_reason": evaluation.non_activation_reason,
        "activation_long_qty": evaluation.activation_long_qty,
        "activation_short_qty": evaluation.activation_short_qty,
        "activation_gap_qty": evaluation.activation_gap_qty,
        "activation_long_avg": evaluation.activation_long_avg,
        "activation_short_avg": evaluation.activation_short_avg,
        "activation_base_main_realized_pnl": evaluation.activation_base_main_realized_pnl,
        "activation_active_cycle_index": evaluation.activation_active_cycle_index,
        "activation_reference_price": evaluation.activation_reference_price,
        "activation_state_source": evaluation.activation_state_source,
        "eligible_for_simulation": evaluation.eligible_for_simulation,
        "diagnostics_errors": list(evaluation.diagnostics_errors),
    }


def is_false_positive(
    *,
    original_status: str,
    original_pnl: float | None,
    recovery_activated: bool,
    original_closed_before_recovery: bool,
) -> bool:
    if not recovery_activated:
        return False
    if original_status != "closed":
        return False
    if original_pnl is None or float(original_pnl) <= 0.0:
        return False
    return not original_closed_before_recovery

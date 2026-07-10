"""Find and export recovery-trade candidates from the original hedge backtester."""

from __future__ import annotations

import csv
import json
import math
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from .backtest_audit_recorder import BacktestAuditRecorder, FillAuditRecord
from .backtest_report import BacktestResult, resolve_net_closed_pnl
from .backtester_trust_audit import export_fill_audit_records
from .candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol_with_slice_info
from .continuous_reentry_backtest import (
    continuous_trade_block_id,
    run_continuous_reentry_for_direction,
)
from .debug_report import calculate_unrealized_pnl
from .historical_backtest import normalize_candles, run_historical_backtest
from .multi_start_backtest import generate_start_indices
from .recovery_wait_activation import (
    RecoveryWaitEvaluation,
    TradeFillReplayRow,
    evaluate_recovery_wait_activation,
    load_trade_fill_replay_rows,
    replay_state_at_absolute_index,
)
from .simulated_execution import resolve_simulated_fee_rate
from .simulated_order_book import SyntheticCandle
from .simulated_pnl import calculate_simulated_closed_pnl
from .trade_block_export import stamp_trade_block_id, write_trade_block_exports

PRIMARY_RECOVERY_PURPOSE = "CYCLE_4_LONG_ADD"
PRIMARY_RECOVERY_WAIT_CANDLES = 576
DEFAULT_SYMBOL = "APTUSDT"
DEFAULT_DIRECTION = "long"
DEFAULT_FILL_MODEL = "conservative"
DEFAULT_CONFIG_SOURCE = "live"
DEFAULT_MIN_FOLLOW_CANDLES = 100
DEFAULT_START_STEP = 100
MIN_CANDLES_FOR_PRIMARY_RECOVERY = 1200

FINAL_EXIT_PURPOSES = frozenset(
    {
        "LONG_TP_EXIT",
        "SHORT_SL_EXIT",
        "LONG_SL_EXIT",
        "SHORT_TP_EXIT",
    }
)

DIAGNOSTIC_RECOVERY_CONFIGS: tuple[tuple[str, int, str], ...] = (
    ("CYCLE_3_SHORT_REDUCE", 576, "diagnostic_cycle3_short_reduce_wait_576"),
    ("CYCLE_4_LONG_ADD", 288, "diagnostic_cycle4_long_add_wait_288"),
)

CANDIDATE_CSV_FIELDS = (
    "trade_block_id",
    "scan_mode",
    "recovery_config_label",
    "reference_purpose",
    "recovery_wait_candles",
    "start_index",
    "trade_number",
    "eligible",
    "rejection_reason",
    "initial_entry_timestamp",
    "initial_entry_price",
    "reference_fill_local_candle_index",
    "reference_fill_absolute_candle_index",
    "reference_fill_timestamp",
    "activation_local_candle_index",
    "activation_absolute_candle_index",
    "activation_timestamp",
    "still_open_at_activation",
    "long_qty_at_activation",
    "short_qty_at_activation",
    "gap_at_activation",
    "realized_pnl_net",
    "total_pnl_at_activation",
    "candles_remaining_after_activation",
)


@dataclass(frozen=True)
class RecoveryScanConfig:
    symbol: str = DEFAULT_SYMBOL
    direction: str = DEFAULT_DIRECTION
    reference_purpose: str = PRIMARY_RECOVERY_PURPOSE
    recovery_wait_candles: int = PRIMARY_RECOVERY_WAIT_CANDLES
    label: str = "primary"
    fill_model: str = DEFAULT_FILL_MODEL
    config_source: str = DEFAULT_CONFIG_SOURCE
    min_follow_candles: int = DEFAULT_MIN_FOLLOW_CANDLES


@dataclass
class RecoveryTradeCandidate:
    scan_mode: str
    trade_block_id: str
    trade_number: int | None
    start_index: int
    symbol: str
    direction: str
    recovery_config_label: str
    reference_purpose: str
    recovery_wait_candles: int
    eligible: bool
    rejection_reason: str | None = None
    initial_entry_timestamp: str | None = None
    initial_entry_price: float | None = None
    reference_fill_local_candle_index: int | None = None
    reference_fill_absolute_candle_index: int | None = None
    reference_fill_timestamp: str | None = None
    reference_fill_price: float | None = None
    activation_local_candle_index: int | None = None
    activation_absolute_candle_index: int | None = None
    activation_timestamp: str | None = None
    still_open_at_activation: bool = False
    long_qty_at_activation: float | None = None
    short_qty_at_activation: float | None = None
    long_avg_at_activation: float | None = None
    short_avg_at_activation: float | None = None
    gap_at_activation: float | None = None
    realized_pnl_net: float | None = None
    unrealized_long_pnl_at_close: float | None = None
    unrealized_short_pnl_at_close: float | None = None
    open_long_entry_fee_remaining: float | None = None
    open_short_entry_fee_remaining: float | None = None
    estimated_joint_exit_fees_at_close: float | None = None
    total_net_pnl_if_closed_at_activation: float | None = None
    total_pnl_at_activation: float | None = None
    candles_remaining_after_activation: int = 0
    validation_errors: list[str] = field(default_factory=list)

    def sort_key(self) -> tuple[int, float, int]:
        return (
            0 if self.eligible else 1,
            -(float(self.gap_at_activation or 0.0)),
            -int(self.candles_remaining_after_activation),
        )


def _safe_float(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return parsed


def _safe_int(value: Any, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def resolve_absolute_candle_index(
    *,
    local_candle_index: int,
    start_index: int,
    input_slice_start_index: int = 0,
) -> int:
    return int(input_slice_start_index) + int(start_index) + int(local_candle_index)


def local_candle_index_from_row(row: dict[str, Any]) -> int | None:
    for key in ("local_candle_index", "candle_index"):
        value = row.get(key)
        if value not in (None, ""):
            parsed = _safe_int(value)
            if parsed is not None:
                return parsed
    return None


def activation_local_candle_index(reference_local: int, recovery_wait_candles: int) -> int:
    return int(reference_local) + int(recovery_wait_candles)


def find_reference_fill_row(
    fill_rows: Sequence[dict[str, Any]],
    purpose: str,
) -> dict[str, Any] | None:
    matches = [
        row
        for row in fill_rows
        if str(row.get("purpose") or "") == purpose
    ]
    return matches[-1] if matches else None


def fill_rows_from_result(result: BacktestResult) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cumulative = 0.0
    for entry in result.fill_log or []:
        closed = resolve_net_closed_pnl(entry)
        cumulative += float(closed or 0.0)
        row = dict(entry)
        row["row_type"] = "fill"
        row["cumulative_realized_pnl_net"] = cumulative
        rows.append(row)
    return rows


def load_fill_replay_rows_from_fill_log(
    fill_rows: Sequence[dict[str, Any]],
    *,
    run_start_index: int,
    input_slice_start_index: int,
) -> list[TradeFillReplayRow]:
    replay_rows: list[TradeFillReplayRow] = []
    for row in fill_rows:
        local_idx = local_candle_index_from_row(row)
        if local_idx is None:
            continue
        absolute_index = resolve_absolute_candle_index(
            local_candle_index=local_idx,
            start_index=run_start_index,
            input_slice_start_index=input_slice_start_index,
        )
        long_qty = row.get("long_qty_after")
        short_qty = row.get("short_qty_after")
        long_avg = row.get("long_avg_after")
        short_avg = row.get("short_avg_after")
        if any(value is None for value in (long_qty, short_qty, long_avg, short_avg)):
            continue
        cumulative = row.get("cumulative_realized_pnl_net")
        if cumulative is None:
            cumulative = row.get("cumulative_pnl")
        replay_rows.append(
            TradeFillReplayRow(
                absolute_candle_index=absolute_index,
                timestamp=str(row.get("timestamp") or "") or None,
                purpose=str(row.get("purpose") or ""),
                fill_price=_safe_float(row.get("fill_price")),
                long_qty_after=float(long_qty),
                short_qty_after=float(short_qty),
                long_avg_after=float(long_avg),
                short_avg_after=float(short_avg),
                cumulative_realized_pnl_net=float(cumulative or 0.0),
                cycle_index=_safe_int(row.get("cycle_index")),
                flat_after_fill=float(long_qty) <= 0.0 and float(short_qty) <= 0.0,
            )
        )
    replay_rows.sort(key=lambda item: (item.absolute_candle_index, item.timestamp or ""))
    return replay_rows


def has_final_exit_before_activation(
    fill_rows: Sequence[dict[str, Any]],
    *,
    activation_local_candle_index: int,
) -> bool:
    for row in fill_rows:
        purpose = str(row.get("purpose") or "")
        if purpose not in FINAL_EXIT_PURPOSES:
            continue
        local_idx = local_candle_index_from_row(row)
        if local_idx is None:
            continue
        if local_idx < activation_local_candle_index:
            return True
    return False


def compute_activation_pnl(
    *,
    replay_state: TradeFillReplayRow,
    reference_price: float,
    fee_rate: float,
) -> dict[str, float]:
    unreal_long, unreal_short, _ = calculate_unrealized_pnl(
        replay_state.long_qty_after,
        replay_state.long_avg_after,
        replay_state.short_qty_after,
        replay_state.short_avg_after,
        reference_price,
    )
    unreal_long = float(unreal_long or 0.0)
    unreal_short = float(unreal_short or 0.0)

    long_close_net = 0.0
    short_close_net = 0.0
    long_entry_fee = 0.0
    short_entry_fee = 0.0
    long_exit_fee = 0.0
    short_exit_fee = 0.0

    if replay_state.long_qty_after > 0:
        long_close_net, long_details = calculate_simulated_closed_pnl(
            side="long",
            avg_entry_price=replay_state.long_avg_after,
            fill_price=reference_price,
            qty=replay_state.long_qty_after,
            reduce_only=True,
            fee_rate=fee_rate,
        )
        long_entry_fee = float(long_details.get("entry_fee") or 0.0)
        long_exit_fee = float(long_details.get("exit_fee") or 0.0)

    if replay_state.short_qty_after > 0:
        short_close_net, short_details = calculate_simulated_closed_pnl(
            side="short",
            avg_entry_price=replay_state.short_avg_after,
            fill_price=reference_price,
            qty=replay_state.short_qty_after,
            reduce_only=True,
            fee_rate=fee_rate,
        )
        short_entry_fee = float(short_details.get("entry_fee") or 0.0)
        short_exit_fee = float(short_details.get("exit_fee") or 0.0)

    realized = float(replay_state.cumulative_realized_pnl_net)
    total_if_closed = realized + float(long_close_net) + float(short_close_net)
    return {
        "realized_pnl_net": realized,
        "unrealized_long_pnl_at_close": unreal_long,
        "unrealized_short_pnl_at_close": unreal_short,
        "open_long_entry_fee_remaining": long_entry_fee,
        "open_short_entry_fee_remaining": short_entry_fee,
        "estimated_joint_exit_fees_at_close": long_exit_fee + short_exit_fee,
        "total_net_pnl_if_closed_at_activation": total_if_closed,
        "total_pnl_at_activation": total_if_closed,
    }


def build_reference_snapshot(
    *,
    reference_fill: dict[str, Any],
    start_index: int,
    input_slice_start_index: int,
) -> dict[str, Any]:
    local_idx = local_candle_index_from_row(reference_fill)
    if local_idx is None:
        raise ValueError("reference fill missing local candle index")
    absolute_idx = resolve_absolute_candle_index(
        local_candle_index=local_idx,
        start_index=start_index,
        input_slice_start_index=input_slice_start_index,
    )
    return {
        "recovery_candle_index": absolute_idx,
        "cycle3_candle_index": absolute_idx,
        "recovery_fill_timestamp": reference_fill.get("timestamp"),
        "cycle3_fill_timestamp": reference_fill.get("timestamp"),
        "recovery_fill_price": reference_fill.get("fill_price"),
        "long_qty_at_recovery_start": reference_fill.get("long_qty_after"),
        "short_qty_at_recovery_start": reference_fill.get("short_qty_after"),
        "long_avg_at_recovery_start": reference_fill.get("long_avg_after"),
        "short_avg_at_recovery_start": reference_fill.get("short_avg_after"),
        "realized_pnl_at_recovery_start": reference_fill.get("cumulative_realized_pnl_net"),
    }


def evaluate_trade_for_recovery(
    result: BacktestResult,
    *,
    candles: list[SyntheticCandle],
    scan_config: RecoveryScanConfig,
    scan_mode: str,
    input_slice_start_index: int,
    fee_rate: float,
) -> RecoveryTradeCandidate:
    start_index = int(result.start_index or 0)
    trade_number = result.trade_number
    trade_block_id = str(result.trade_block_id or continuous_trade_block_id(scan_config.direction, trade_number or 1))
    fill_rows = fill_rows_from_result(result)
    candidate = RecoveryTradeCandidate(
        scan_mode=scan_mode,
        trade_block_id=trade_block_id,
        trade_number=trade_number,
        start_index=start_index,
        symbol=scan_config.symbol,
        direction=scan_config.direction,
        recovery_config_label=scan_config.label,
        reference_purpose=scan_config.reference_purpose,
        recovery_wait_candles=scan_config.recovery_wait_candles,
        eligible=False,
    )

    initial_fill = find_reference_fill_row(fill_rows, "INITIAL_LONG_ENTRY")
    if initial_fill is not None:
        candidate.initial_entry_timestamp = str(initial_fill.get("timestamp") or "") or None
        candidate.initial_entry_price = _safe_float(initial_fill.get("fill_price"))

    reference_fill = find_reference_fill_row(fill_rows, scan_config.reference_purpose)
    if reference_fill is None:
        candidate.rejection_reason = "reference_fill_not_found"
        return candidate

    ref_local = local_candle_index_from_row(reference_fill)
    if ref_local is None:
        candidate.rejection_reason = "reference_fill_missing_local_index"
        return candidate

    candidate.reference_fill_local_candle_index = ref_local
    candidate.reference_fill_absolute_candle_index = resolve_absolute_candle_index(
        local_candle_index=ref_local,
        start_index=start_index,
        input_slice_start_index=input_slice_start_index,
    )
    candidate.reference_fill_timestamp = str(reference_fill.get("timestamp") or "") or None
    candidate.reference_fill_price = _safe_float(reference_fill.get("fill_price"))

    activation_local = activation_local_candle_index(ref_local, scan_config.recovery_wait_candles)
    activation_absolute = resolve_absolute_candle_index(
        local_candle_index=activation_local,
        start_index=start_index,
        input_slice_start_index=input_slice_start_index,
    )
    candidate.activation_local_candle_index = activation_local
    candidate.activation_absolute_candle_index = activation_absolute
    candidate.candles_remaining_after_activation = max(0, len(candles) - activation_absolute - 1)

    if activation_absolute >= len(candles):
        candidate.rejection_reason = "series_ended_before_activation"
        return candidate

    if candidate.candles_remaining_after_activation < scan_config.min_follow_candles:
        candidate.rejection_reason = "insufficient_follow_candles"
        return candidate

    if has_final_exit_before_activation(
        fill_rows,
        activation_local_candle_index=activation_local,
    ):
        candidate.rejection_reason = "final_exit_before_activation"
        return candidate

    activation_candle = candles[activation_absolute]
    candidate.activation_timestamp = (
        activation_candle.timestamp.isoformat() if activation_candle.timestamp is not None else None
    )
    reference_price = float(activation_candle.close)

    replay_rows = load_fill_replay_rows_from_fill_log(
        fill_rows,
        run_start_index=start_index,
        input_slice_start_index=input_slice_start_index,
    )
    replay_state = replay_state_at_absolute_index(replay_rows, activation_absolute)
    if replay_state is None:
        candidate.rejection_reason = "activation_state_unavailable"
        return candidate

    if replay_state.flat_after_fill:
        candidate.rejection_reason = "trade_closed_before_activation"
        candidate.still_open_at_activation = False
        return candidate

    gap = max(replay_state.long_qty_after - replay_state.short_qty_after, 0.0)
    candidate.still_open_at_activation = True
    candidate.long_qty_at_activation = replay_state.long_qty_after
    candidate.short_qty_at_activation = replay_state.short_qty_after
    candidate.long_avg_at_activation = replay_state.long_avg_after
    candidate.short_avg_at_activation = replay_state.short_avg_after
    candidate.gap_at_activation = gap

    pnl = compute_activation_pnl(
        replay_state=replay_state,
        reference_price=reference_price,
        fee_rate=fee_rate,
    )
    candidate.realized_pnl_net = pnl["realized_pnl_net"]
    candidate.unrealized_long_pnl_at_close = pnl["unrealized_long_pnl_at_close"]
    candidate.unrealized_short_pnl_at_close = pnl["unrealized_short_pnl_at_close"]
    candidate.open_long_entry_fee_remaining = pnl["open_long_entry_fee_remaining"]
    candidate.open_short_entry_fee_remaining = pnl["open_short_entry_fee_remaining"]
    candidate.estimated_joint_exit_fees_at_close = pnl["estimated_joint_exit_fees_at_close"]
    candidate.total_net_pnl_if_closed_at_activation = pnl["total_net_pnl_if_closed_at_activation"]
    candidate.total_pnl_at_activation = pnl["total_pnl_at_activation"]

    if gap <= 0.0:
        candidate.rejection_reason = "non_positive_long_gap"
        return candidate

    candidate.eligible = True
    candidate.rejection_reason = None
    return candidate


def validate_selected_candidate(
    candidate: RecoveryTradeCandidate,
    *,
    fill_rows: Sequence[dict[str, Any]],
    candles: list[SyntheticCandle],
    input_slice_start_index: int,
    fee_rate: float,
) -> list[str]:
    errors: list[str] = []
    reference_fill = find_reference_fill_row(fill_rows, candidate.reference_purpose)
    if reference_fill is None:
        errors.append("reference_fill_not_found")
        return errors

    ref_local = candidate.reference_fill_local_candle_index
    activation_local = candidate.activation_local_candle_index
    if ref_local is None or activation_local is None:
        errors.append("missing_local_indices")
        return errors

    if activation_local - ref_local != candidate.recovery_wait_candles:
        errors.append("activation_wait_mismatch")

    if has_final_exit_before_activation(fill_rows, activation_local_candle_index=activation_local):
        errors.append("final_exit_before_activation")

    activation_absolute = candidate.activation_absolute_candle_index
    if activation_absolute is None or activation_absolute >= len(candles):
        errors.append("activation_index_out_of_range")
        return errors

    replay_rows = load_fill_replay_rows_from_fill_log(
        fill_rows,
        run_start_index=candidate.start_index,
        input_slice_start_index=input_slice_start_index,
    )
    replay_state = replay_state_at_absolute_index(replay_rows, activation_absolute)
    if replay_state is None:
        errors.append("activation_replay_state_missing")
        return errors

    if replay_state.flat_after_fill:
        errors.append("trade_not_open_at_activation")

    gap = max(replay_state.long_qty_after - replay_state.short_qty_after, 0.0)
    if gap <= 0.0:
        errors.append("non_positive_gap")

    if candidate.long_qty_at_activation is not None:
        if abs(replay_state.long_qty_after - candidate.long_qty_at_activation) > 1e-6:
            errors.append("long_qty_mismatch")

    if candidate.gap_at_activation is not None and abs(gap - candidate.gap_at_activation) > 1e-6:
        errors.append("gap_mismatch")

    realized_sum = sum(float(resolve_net_closed_pnl(row) or 0.0) for row in fill_rows)
    fills_before_activation = [
        row
        for row in fill_rows
        if (local_candle_index_from_row(row) or 10**9) <= activation_local
    ]
    realized_to_activation = sum(float(resolve_net_closed_pnl(row) or 0.0) for row in fills_before_activation)
    if candidate.realized_pnl_net is not None:
        if abs(realized_to_activation - candidate.realized_pnl_net) > 1e-4:
            errors.append("realized_pnl_mismatch")

    reference_price = float(candles[activation_absolute].close)
    pnl = compute_activation_pnl(
        replay_state=replay_state,
        reference_price=reference_price,
        fee_rate=fee_rate,
    )
    if candidate.total_net_pnl_if_closed_at_activation is not None:
        if abs(pnl["total_net_pnl_if_closed_at_activation"] - candidate.total_net_pnl_if_closed_at_activation) > 1e-4:
            errors.append("total_pnl_mismatch")

    if candidate.candles_remaining_after_activation < DEFAULT_MIN_FOLLOW_CANDLES:
        errors.append("insufficient_follow_candles")

    if abs((candidate.long_qty_at_activation or 0.0) - (candidate.short_qty_at_activation or 0.0) - (candidate.gap_at_activation or 0.0)) > 1e-6:
        errors.append("initial_gap_formula_mismatch")

    return errors


def filter_viable_start_indices(
    start_indices: Iterable[int],
    *,
    candle_count: int,
    min_room_candles: int = MIN_CANDLES_FOR_PRIMARY_RECOVERY,
) -> list[int]:
    last_viable = max(0, candle_count - min_room_candles)
    return [int(value) for value in start_indices if 0 <= int(value) <= last_viable]


def scan_start_indices(
    *,
    candles: list[SyntheticCandle],
    start_indices: Iterable[int],
    scan_config: RecoveryScanConfig,
    input_slice_start_index: int = 0,
    fee_rate: float | None = None,
) -> list[RecoveryTradeCandidate]:
    fee = float(fee_rate if fee_rate is not None else resolve_simulated_fee_rate())
    candidates: list[RecoveryTradeCandidate] = []
    for start_index in start_indices:
        if start_index < 0 or start_index >= len(candles):
            continue
        results = run_continuous_reentry_for_direction(
            scan_config.symbol,
            scan_config.direction,
            candles,
            continuous_start_index=int(start_index),
            continuous_max_trades=1,
            config_source=scan_config.config_source,  # type: ignore[arg-type]
            fill_model=scan_config.fill_model,
            recovery_bot_config=None,
            input_slice_start_index=input_slice_start_index,
        )
        if not results:
            continue
        candidate = evaluate_trade_for_recovery(
            results[0],
            candles=candles,
            scan_config=scan_config,
            scan_mode="start_index",
            input_slice_start_index=input_slice_start_index,
            fee_rate=fee,
        )
        candidates.append(candidate)
    return candidates


def scan_continuous_trades(
    *,
    candles: list[SyntheticCandle],
    continuous_start_index: int,
    max_trades: int,
    scan_config: RecoveryScanConfig,
    input_slice_start_index: int = 0,
    fee_rate: float | None = None,
) -> list[RecoveryTradeCandidate]:
    fee = float(fee_rate if fee_rate is not None else resolve_simulated_fee_rate())
    results = run_continuous_reentry_for_direction(
        scan_config.symbol,
        scan_config.direction,
        candles,
        continuous_start_index=continuous_start_index,
        continuous_max_trades=max_trades,
        config_source=scan_config.config_source,  # type: ignore[arg-type]
        fill_model=scan_config.fill_model,
        recovery_bot_config=None,
        input_slice_start_index=input_slice_start_index,
    )
    return [
        evaluate_trade_for_recovery(
            result,
            candles=candles,
            scan_config=scan_config,
            scan_mode="continuous_trade",
            input_slice_start_index=input_slice_start_index,
            fee_rate=fee,
        )
        for result in results
    ]


def sort_candidates(candidates: Iterable[RecoveryTradeCandidate]) -> list[RecoveryTradeCandidate]:
    return sorted(candidates, key=lambda item: item.sort_key())


def candidate_to_csv_row(candidate: RecoveryTradeCandidate) -> dict[str, Any]:
    return {
        "trade_block_id": candidate.trade_block_id,
        "scan_mode": candidate.scan_mode,
        "recovery_config_label": candidate.recovery_config_label,
        "reference_purpose": candidate.reference_purpose,
        "recovery_wait_candles": candidate.recovery_wait_candles,
        "start_index": candidate.start_index,
        "trade_number": candidate.trade_number,
        "eligible": candidate.eligible,
        "rejection_reason": candidate.rejection_reason or "",
        "initial_entry_timestamp": candidate.initial_entry_timestamp or "",
        "initial_entry_price": candidate.initial_entry_price,
        "reference_fill_local_candle_index": candidate.reference_fill_local_candle_index,
        "reference_fill_absolute_candle_index": candidate.reference_fill_absolute_candle_index,
        "reference_fill_timestamp": candidate.reference_fill_timestamp or "",
        "activation_local_candle_index": candidate.activation_local_candle_index,
        "activation_absolute_candle_index": candidate.activation_absolute_candle_index,
        "activation_timestamp": candidate.activation_timestamp or "",
        "still_open_at_activation": candidate.still_open_at_activation,
        "long_qty_at_activation": candidate.long_qty_at_activation,
        "short_qty_at_activation": candidate.short_qty_at_activation,
        "gap_at_activation": candidate.gap_at_activation,
        "realized_pnl_net": candidate.realized_pnl_net,
        "total_pnl_at_activation": candidate.total_pnl_at_activation,
        "candles_remaining_after_activation": candidate.candles_remaining_after_activation,
    }


def write_candidate_exports(
    output_dir: Path,
    *,
    candidates: list[RecoveryTradeCandidate],
    scan_metadata: dict[str, Any],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = [candidate_to_csv_row(candidate) for candidate in candidates]
    with (output_dir / "recovery_trade_candidates.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CANDIDATE_CSV_FIELDS))
        writer.writeheader()
        writer.writerows(rows)

    payload = {
        "metadata": scan_metadata,
        "candidates": [asdict(candidate) for candidate in candidates],
    }
    (output_dir / "recovery_trade_candidates.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def build_report_markdown(
    *,
    scan_metadata: dict[str, Any],
    candidates: list[RecoveryTradeCandidate],
    selected: RecoveryTradeCandidate | None,
    diagnostic_candidates: list[RecoveryTradeCandidate],
) -> str:
    eligible = [candidate for candidate in candidates if candidate.eligible]
    with_reference = [
        candidate
        for candidate in candidates
        if candidate.reference_fill_local_candle_index is not None
    ]
    still_open = [
        candidate
        for candidate in with_reference
        if candidate.still_open_at_activation
    ]
    lines = [
        "# Recovery Trade Candidate Scan",
        "",
        f"- Tested start indices: **{scan_metadata.get('tested_start_indices_count', 0)}**",
        f"- Trades with `{PRIMARY_RECOVERY_PURPOSE}` fill: **{len(with_reference)}**",
        f"- Still open after {PRIMARY_RECOVERY_WAIT_CANDLES} candles: **{len(still_open)}**",
        f"- Eligible primary candidates: **{len(eligible)}**",
        "",
        "## Eligible candidates",
        "",
        "| Trade | Start | C4 Fill | Activation | Open? | Long Qty | Short Qty | Gap | Realized PnL | Total PnL | Candles left |",
        "| ----- | ----: | ------: | ---------: | ----- | -------: | --------: | --: | -----------: | --------: | -----------: |",
    ]
    if not eligible:
        lines.append("| — | — | — | — | — | — | — | — | — | — | — |")
    else:
        for candidate in sort_candidates(eligible):
            lines.append(
                f"| {candidate.trade_block_id} | {candidate.start_index} | "
                f"{candidate.reference_fill_local_candle_index} | "
                f"{candidate.activation_local_candle_index} | yes | "
                f"{candidate.long_qty_at_activation:.4f} | {candidate.short_qty_at_activation:.4f} | "
                f"{candidate.gap_at_activation:.4f} | {candidate.realized_pnl_net:.6f} | "
                f"{candidate.total_pnl_at_activation:.6f} | {candidate.candles_remaining_after_activation} |"
            )

    if selected is not None:
        lines.extend(
            [
                "",
                "## Selected trade",
                "",
                f"- Trade ID: `{selected.trade_block_id}`",
                f"- Start index: `{selected.start_index}`",
                f"- Activation absolute candle: `{selected.activation_absolute_candle_index}`",
                f"- Gap: `{selected.gap_at_activation}`",
            ]
        )

    if diagnostic_candidates:
        lines.extend(["", "## Diagnostic variants (not primary config)", ""])
        for candidate in diagnostic_candidates[:10]:
            lines.append(
                f"- `{candidate.recovery_config_label}` start={candidate.start_index} "
                f"eligible={candidate.eligible} reason={candidate.rejection_reason or 'ok'}"
            )
    return "\n".join(lines) + "\n"


def _candle_row(
    candle: SyntheticCandle,
    *,
    local_candle_index: int,
    absolute_candle_index: int,
    sequence_after_activation: int | None = None,
) -> dict[str, Any]:
    row = {
        "local_candle_index": local_candle_index,
        "absolute_candle_index": absolute_candle_index,
        "timestamp": candle.timestamp.isoformat() if candle.timestamp is not None else "",
        "open": float(candle.open if candle.open is not None else candle.close),
        "high": float(candle.high if candle.high is not None else candle.close),
        "low": float(candle.low if candle.low is not None else candle.close),
        "close": float(candle.close),
    }
    if sequence_after_activation is not None:
        row["sequence_after_activation"] = sequence_after_activation
    return row


def export_candles_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_recovery_start_snapshot(
    candidate: RecoveryTradeCandidate,
    *,
    candles: list[SyntheticCandle],
    input_slice_start_index: int,
) -> dict[str, Any]:
    activation_absolute = int(candidate.activation_absolute_candle_index or 0)
    reference_price = float(candles[activation_absolute].close)
    return {
        "trade_block_id": candidate.trade_block_id,
        "direction": candidate.direction,
        "symbol": candidate.symbol,
        "start_index": candidate.start_index,
        "input_slice_start_index": input_slice_start_index,
        "reference_fill_purpose": candidate.reference_purpose,
        "reference_fill_local_candle_index": candidate.reference_fill_local_candle_index,
        "reference_fill_absolute_candle_index": candidate.reference_fill_absolute_candle_index,
        "reference_fill_timestamp": candidate.reference_fill_timestamp,
        "recovery_wait_candles": candidate.recovery_wait_candles,
        "activation_local_candle_index": candidate.activation_local_candle_index,
        "activation_absolute_candle_index": candidate.activation_absolute_candle_index,
        "activation_timestamp": candidate.activation_timestamp,
        "reference_price": reference_price,
        "long_qty": candidate.long_qty_at_activation,
        "short_qty": candidate.short_qty_at_activation,
        "long_avg": candidate.long_avg_at_activation,
        "short_avg": candidate.short_avg_at_activation,
        "initial_gap": candidate.gap_at_activation,
        "realized_pnl_net": candidate.realized_pnl_net,
        "open_long_entry_fee_remaining": candidate.open_long_entry_fee_remaining,
        "open_short_entry_fee_remaining": candidate.open_short_entry_fee_remaining,
        "unrealized_long_pnl_at_close": candidate.unrealized_long_pnl_at_close,
        "unrealized_short_pnl_at_close": candidate.unrealized_short_pnl_at_close,
        "estimated_joint_exit_fees_at_close": candidate.estimated_joint_exit_fees_at_close,
        "total_net_pnl_if_closed_at_activation": candidate.total_net_pnl_if_closed_at_activation,
    }


def export_selected_trade(
    *,
    candidate: RecoveryTradeCandidate,
    candles: list[SyntheticCandle],
    output_dir: Path,
    input_slice_start_index: int,
    fee_rate: float | None = None,
) -> dict[str, Any]:
    fee = float(fee_rate if fee_rate is not None else resolve_simulated_fee_rate())
    output_dir.mkdir(parents=True, exist_ok=True)
    selected_dir = output_dir / "selected_trade"
    if selected_dir.exists():
        shutil.rmtree(selected_dir)
    selected_dir.mkdir(parents=True, exist_ok=True)

    slice_candles = candles[candidate.start_index :]
    recorder = BacktestAuditRecorder(enabled=True)
    result = run_historical_backtest(
        candidate.symbol,
        candidate.direction,
        slice_candles,
        fill_model=DEFAULT_FILL_MODEL,
        config_source=DEFAULT_CONFIG_SOURCE,  # type: ignore[arg-type]
        audit_recorder=recorder,
        absolute_trade_start_index=candidate.start_index,
        input_slice_start_index=input_slice_start_index,
    )
    result.trade_number = candidate.trade_number or 1
    stamp_trade_block_id(result, candidate.trade_block_id)
    result.start_index = candidate.start_index
    result.input_slice_start_index = input_slice_start_index

    fill_rows = fill_rows_from_result(result)
    validation_errors = validate_selected_candidate(
        candidate,
        fill_rows=fill_rows,
        candles=candles,
        input_slice_start_index=input_slice_start_index,
        fee_rate=fee,
    )
    if validation_errors:
        raise RuntimeError(f"selected trade failed validation: {validation_errors}")

    trade_exports = write_trade_block_exports(result, selected_dir)
    export_fill_audit_records(selected_dir / "fill_audit.json", recorder)

    snapshot = build_recovery_start_snapshot(
        candidate,
        candles=candles,
        input_slice_start_index=input_slice_start_index,
    )
    (selected_dir / "recovery_start_snapshot.json").write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    trade_metadata = {
        "trade_block_id": candidate.trade_block_id,
        "symbol": candidate.symbol,
        "direction": candidate.direction,
        "start_index": candidate.start_index,
        "input_slice_start_index": input_slice_start_index,
        "fill_model": DEFAULT_FILL_MODEL,
        "config_source": DEFAULT_CONFIG_SOURCE,
        "recovery_bot_enabled": False,
        "reference_purpose": candidate.reference_purpose,
        "recovery_wait_candles": candidate.recovery_wait_candles,
        "activation_absolute_candle_index": candidate.activation_absolute_candle_index,
        "candles_remaining_after_activation": candidate.candles_remaining_after_activation,
        "validation_passed": True,
        "trade_export_files": trade_exports,
    }
    (selected_dir / "trade_metadata.json").write_text(
        json.dumps(trade_metadata, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    trade_start_rows = [
        _candle_row(
            candles[absolute_idx],
            local_candle_index=absolute_idx - candidate.start_index,
            absolute_candle_index=absolute_idx,
        )
        for absolute_idx in range(candidate.start_index, len(candles))
    ]
    export_candles_csv(selected_dir / "candles_from_trade_start.csv", trade_start_rows)

    activation_absolute = int(candidate.activation_absolute_candle_index or 0)
    activation_rows = [
        _candle_row(
            candles[absolute_idx],
            local_candle_index=absolute_idx - candidate.start_index,
            absolute_candle_index=absolute_idx,
            sequence_after_activation=absolute_idx - activation_absolute,
        )
        for absolute_idx in range(activation_absolute, len(candles))
    ]
    export_candles_csv(selected_dir / "candles_from_recovery_activation.csv", activation_rows)

    replay_rows = load_fill_replay_rows_from_fill_log(
        fill_rows,
        run_start_index=candidate.start_index,
        input_slice_start_index=input_slice_start_index,
    )
    activation_absolute = int(candidate.activation_absolute_candle_index or 0)
    replay_state = replay_state_at_absolute_index(replay_rows, activation_absolute)

    position_payload = {
        "trusted": not validation_errors,
        "validation_errors": validation_errors,
        "activation_absolute_candle_index": activation_absolute,
        "activation_local_candle_index": candidate.activation_local_candle_index,
        "activation_replay_long_qty": replay_state.long_qty_after if replay_state else None,
        "activation_replay_short_qty": replay_state.short_qty_after if replay_state else None,
        "activation_replay_gap": (
            max(replay_state.long_qty_after - replay_state.short_qty_after, 0.0)
            if replay_state
            else None
        ),
        "snapshot_long_qty": candidate.long_qty_at_activation,
        "snapshot_short_qty": candidate.short_qty_at_activation,
        "snapshot_gap": candidate.gap_at_activation,
        "reference_fill_local_candle_index": candidate.reference_fill_local_candle_index,
        "wait_candles_observed": (
            int(candidate.activation_local_candle_index or 0)
            - int(candidate.reference_fill_local_candle_index or 0)
            if candidate.activation_local_candle_index is not None
            and candidate.reference_fill_local_candle_index is not None
            else None
        ),
        "final_exit_before_activation": has_final_exit_before_activation(
            fill_rows,
            activation_local_candle_index=int(candidate.activation_local_candle_index or 0),
        ),
    }
    (selected_dir / "position_reconciliation.json").write_text(
        json.dumps(position_payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    pnl_payload = {
        "fee_rate": fee,
        "realized_pnl_net": candidate.realized_pnl_net,
        "total_net_pnl_if_closed_at_activation": candidate.total_net_pnl_if_closed_at_activation,
        "open_long_entry_fee_remaining": candidate.open_long_entry_fee_remaining,
        "open_short_entry_fee_remaining": candidate.open_short_entry_fee_remaining,
        "estimated_joint_exit_fees_at_close": candidate.estimated_joint_exit_fees_at_close,
        "unrealized_long_pnl_at_close": candidate.unrealized_long_pnl_at_close,
        "unrealized_short_pnl_at_close": candidate.unrealized_short_pnl_at_close,
        "reference_price": snapshot["reference_price"],
        "validation_errors": validation_errors,
    }
    (selected_dir / "pnl_reconciliation.json").write_text(
        json.dumps(pnl_payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    trade_blocks_json = next(selected_dir.glob("*trade_blocks.json"), None)
    if trade_blocks_json is not None:
        target_json = selected_dir / "trade_blocks.json"
        if trade_blocks_json.name != target_json.name:
            shutil.copy2(trade_blocks_json, target_json)
        target_csv = selected_dir / "trade_blocks.csv"
        source_csv = trade_blocks_json.with_suffix(".csv")
        if source_csv.is_file() and not target_csv.is_file():
            shutil.copy2(source_csv, target_csv)

    return {
        "selected_dir": str(selected_dir),
        "recovery_start_snapshot": str(selected_dir / "recovery_start_snapshot.json"),
        "candles_from_recovery_activation": str(selected_dir / "candles_from_recovery_activation.csv"),
        "validation_errors": validation_errors,
    }


def _load_trade_block_rows(selected_dir: Path) -> list[dict[str, Any]]:
    json_path = selected_dir / "trade_blocks.json"
    if not json_path.is_file():
        matches = list(selected_dir.glob("*trade_blocks.json"))
        if not matches:
            return []
        json_path = matches[0]
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        return list(payload.get("trade_blocks") or payload.get("rows") or [])
    return list(payload)


def load_hint_start_indices_from_archive(archive_root: Path | None = None) -> list[int]:
    if archive_root is None:
        archive_root = Path("research/backtests/results/archive_before_backtester_trust_audit_20260710T071643Z")
    if not archive_root.is_dir():
        return []
    hints: set[int] = {0}
    for path in archive_root.rglob("*continuous_results.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        metadata = payload.get("metadata") or {}
        for value in metadata.get("start_indices") or []:
            try:
                hints.add(int(value))
            except (TypeError, ValueError):
                pass
        for run in payload.get("runs") or []:
            ref_abs = run.get("recovery_reference_absolute_candle_index")
            if ref_abs is not None:
                try:
                    hints.add(max(0, int(ref_abs) - 200))
                except (TypeError, ValueError):
                    pass
    return sorted(hints)


def run_recovery_trade_finder(
    *,
    output_dir: Path,
    candles: list[SyntheticCandle],
    input_slice_start_index: int = 0,
    start_step: int = DEFAULT_START_STEP,
    min_follow_candles: int = DEFAULT_MIN_FOLLOW_CANDLES,
    extra_start_indices: Iterable[int] | None = None,
    skip_start_index_scan: bool = False,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    fee_rate = resolve_simulated_fee_rate()

    primary_config = RecoveryScanConfig(min_follow_candles=min_follow_candles)

    raw_start_indices = generate_start_indices(
        len(candles),
        start_step_candles=start_step,
        window_candles=1,
        max_starts=max(1, (len(candles) + start_step - 1) // start_step),
    )
    if extra_start_indices:
        raw_start_indices = sorted(set(raw_start_indices).union(int(value) for value in extra_start_indices))
    start_indices = filter_viable_start_indices(raw_start_indices, candle_count=len(candles))

    # Continuous re-entry from 0 finds stuck/open trades quickly and matches prior recovery audits.
    candidates = scan_continuous_trades(
        candles=candles,
        continuous_start_index=0,
        max_trades=400,
        scan_config=primary_config,
        input_slice_start_index=input_slice_start_index,
        fee_rate=fee_rate,
    )
    eligible = [candidate for candidate in candidates if candidate.eligible]

    start_index_candidates: list[RecoveryTradeCandidate] = []
    if not skip_start_index_scan:
        start_index_candidates = scan_start_indices(
            candles=candles,
            start_indices=start_indices,
            scan_config=primary_config,
            input_slice_start_index=input_slice_start_index,
            fee_rate=fee_rate,
        )
        candidates.extend(start_index_candidates)
    if not eligible:
        eligible = [candidate for candidate in candidates if candidate.eligible]
    fallback_steps: list[int] = []

    if not eligible:
        for step in (50, 25, 10, 1):
            if step >= start_step:
                continue
            fallback_steps.append(step)
            more_indices = filter_viable_start_indices(
                generate_start_indices(
                    len(candles),
                    start_step_candles=step,
                    window_candles=1,
                    max_starts=max(1, len(candles) // step),
                ),
                candle_count=len(candles),
            )
            more = scan_start_indices(
                candles=candles,
                start_indices=more_indices,
                scan_config=primary_config,
                input_slice_start_index=input_slice_start_index,
                fee_rate=fee_rate,
            )
            candidates.extend(more)
            eligible = [candidate for candidate in candidates if candidate.eligible]
            if eligible:
                break

    if not eligible:
        continuous_candidates = scan_continuous_trades(
            candles=candles,
            continuous_start_index=0,
            max_trades=800,
            scan_config=primary_config,
            input_slice_start_index=input_slice_start_index,
            fee_rate=fee_rate,
        )
        candidates.extend(continuous_candidates)
        eligible = [candidate for candidate in candidates if candidate.eligible]

    candidates = sort_candidates(candidates)
    selected = eligible[0] if eligible else None

    diagnostic_candidates: list[RecoveryTradeCandidate] = []
    if not eligible:
        for purpose, wait, label in DIAGNOSTIC_RECOVERY_CONFIGS:
            diagnostic_candidates.extend(
                scan_start_indices(
                    candles=candles,
                    start_indices=start_indices[: min(50, len(start_indices))],
                    scan_config=RecoveryScanConfig(
                        reference_purpose=purpose,
                        recovery_wait_candles=wait,
                        label=label,
                        min_follow_candles=min_follow_candles,
                    ),
                    input_slice_start_index=input_slice_start_index,
                    fee_rate=fee_rate,
                )
            )

    with_reference = [
        candidate
        for candidate in candidates
        if candidate.reference_fill_local_candle_index is not None
        and candidate.reference_purpose == PRIMARY_RECOVERY_PURPOSE
        and candidate.recovery_wait_candles == PRIMARY_RECOVERY_WAIT_CANDLES
    ]
    still_open = [
        candidate
        for candidate in with_reference
        if candidate.still_open_at_activation
    ]

    scan_metadata = {
        "symbol": DEFAULT_SYMBOL,
        "direction": DEFAULT_DIRECTION,
        "candle_count": len(candles),
        "input_slice_start_index": input_slice_start_index,
        "start_step": start_step,
        "fallback_steps": fallback_steps,
        "tested_start_indices_count": len(start_indices),
        "start_index_scan_skipped": skip_start_index_scan,
        "start_index_scan_count": len(start_index_candidates),
        "primary_reference_purpose": PRIMARY_RECOVERY_PURPOSE,
        "primary_recovery_wait_candles": PRIMARY_RECOVERY_WAIT_CANDLES,
        "fee_rate": fee_rate,
        "min_follow_candles": min_follow_candles,
    }
    write_candidate_exports(output_dir, candidates=candidates, scan_metadata=scan_metadata)
    (output_dir / "REPORT.md").write_text(
        build_report_markdown(
            scan_metadata=scan_metadata,
            candidates=candidates,
            selected=selected,
            diagnostic_candidates=diagnostic_candidates,
        ),
        encoding="utf-8",
    )

    export_info: dict[str, Any] | None = None
    if selected is not None:
        export_info = export_selected_trade(
            candidate=selected,
            candles=candles,
            output_dir=output_dir,
            input_slice_start_index=input_slice_start_index,
            fee_rate=fee_rate,
        )

    return {
        "tested_start_indices": len(start_indices),
        "c4_fill_count": len(with_reference),
        "still_open_after_wait_count": len(still_open),
        "eligible_count": len(eligible),
        "selected": asdict(selected) if selected is not None else None,
        "export": export_info,
        "output_dir": str(output_dir),
    }

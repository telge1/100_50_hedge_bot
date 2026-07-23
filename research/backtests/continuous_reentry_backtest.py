"""Continuous re-entry backtests: chain closed trades within one candle window."""

from __future__ import annotations

import csv
import json
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .backtest_config_loader import (
    DEFAULT_LONG_CONFIG_PATH,
    DEFAULT_SHORT_CONFIG_PATH,
    ConfigSource,
)
from .backtest_report import BacktestResult
from .fill_models import resolve_fill_model_config
from .backtest_report import resolve_net_closed_pnl
from .historical_backtest import normalize_candles, run_historical_backtest
from .pnl_coverage_audit import apply_trade_exit_quality
from .multi_start_backtest import compact_result_dict, resolve_directions
from .trade_block_export import ensure_backtest_trade_block_ids, stamp_trade_block_id
from .addon_short_recovery import AddonShortRecoveryConfig
from .exit_rebuild_policy import ExitRebuildPolicyConfig
from .inventory_mtm_freeze import InventoryMtmFreezeConfig
from .recovery_bot_config import RecoveryBotConfig, recovery_bot_config_dict
from .recovery_reentry_policy import (
    RecoveryReentryConfig,
    RecoveryReentryRuntimeState,
    apply_recovery_policy_after_trade,
)

CONTINUOUS_SUMMARY_CSV_FIELDS = (
    "symbol",
    "direction",
    "trade_number",
    "trade_block_id",
    "start_index",
    "end_index",
    "start_time",
    "end_time",
    "candles_processed",
    "final_status",
    "exit_reason",
    "realized_pnl",
    "fills_count",
    "cycles_seen",
    "final_long_qty",
    "final_short_qty",
    "active_orders_count",
    "final_active_order_purposes",
    "exit_quality",
    "recovery_bot_enabled",
    "recovery_activated",
    "recovery_reference_timestamp",
    "recovery_activation_timestamp",
    "recovery_exit_timestamp",
    "recovery_final_pnl",
    "recovery_gap_fully_closed",
)

CONTINUOUS_AGGREGATE_CSV_FIELDS = (
    "symbol",
    "direction",
    "fill_model",
    "config_source",
    "trades_started",
    "closed_count",
    "successful_closed_count",
    "undercovered_final_exit_count",
    "negative_pnl_closed_count",
    "unfinished_count",
    "open_count",
    "error_count",
    "max_candles_count",
    "closed_rate_pct",
    "successful_closed_rate_pct",
    "total_pnl",
    "avg_pnl",
    "median_pnl",
    "best_pnl",
    "worst_pnl",
    "avg_duration_candles",
    "total_candles_processed",
    "first_start_time",
    "last_end_time",
    "normal_closed_count",
    "recovery_activated_count",
    "recovery_closed_count",
    "recovery_failed_count",
    "open_after_recovery_count",
    "recovery_false_positive_candidate_count",
    "total_normal_pnl",
    "total_recovery_trade_pnl",
    "avg_recovery_duration_candles",
    "max_recovery_duration_candles",
)

CONTINUOUS_SUCCESSFUL_EXIT_REASONS = frozenset(
    {
        "flat_no_active_orders",
        "recovery_joint_exit",
    }
)


def continuous_trade_block_id(direction: str, trade_number: int) -> str:
    return f"backtest_{direction}_continuous_trade_{trade_number:04d}"


def _format_timestamp(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat()


def _purposes_joined(purposes: Iterable[str] | None) -> str:
    if not purposes:
        return ""
    return "|".join(str(purpose) for purpose in purposes if purpose)


def _trade_end_index(start_index: int, result: BacktestResult) -> int:
    return int(start_index) + int(result.candles_processed or 0)


def continuous_result_to_summary_row(result: BacktestResult) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for key in CONTINUOUS_SUMMARY_CSV_FIELDS:
        if key == "final_active_order_purposes":
            row[key] = _purposes_joined(result.final_active_order_purposes)
            continue
        if key in {"start_time", "end_time"}:
            row[key] = _format_timestamp(getattr(result, key, None))
            continue
        value = getattr(result, key, None)
        row[key] = "" if value is None else value
    return row


def aggregate_continuous_results(results: Iterable[BacktestResult]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], list[BacktestResult]] = {}
    for result in results:
        key = (
            result.symbol,
            result.direction,
            result.fill_model,
            result.config_source,
        )
        grouped.setdefault(key, []).append(result)

    aggregates: list[dict[str, Any]] = []
    for key in sorted(grouped):
        symbol, direction, fill_model, config_source = key
        runs = grouped[key]
        trade_count = len(runs)
        successful_closed = [run for run in runs if run.exit_quality == "closed_ok"]
        undercovered_closed = [
            run for run in runs if run.exit_quality == "closed_undercovered_final_exit"
        ]
        negative_closed = [run for run in runs if run.exit_quality == "closed_negative_pnl"]
        flat_closed = successful_closed + undercovered_closed + negative_closed
        open_runs = [run for run in runs if run.final_status == "open"]
        max_candles_runs = [run for run in runs if run.final_status == "max_candles"]
        errors = [run for run in runs if run.final_status == "error"]
        unfinished = [
            run
            for run in runs
            if run.exit_quality
            not in {
                "closed_ok",
                "closed_undercovered_final_exit",
                "closed_negative_pnl",
            }
        ]

        pnls = [float(run.realized_pnl) for run in runs]
        durations = [int(run.candles_processed) for run in runs]
        closed_durations = [int(run.candles_processed) for run in flat_closed]
        total_candles = sum(durations)

        start_times = [run.start_time for run in runs if run.start_time is not None]
        end_times = [run.end_time for run in runs if run.end_time is not None]

        closed_count = len(flat_closed)
        successful_closed_count = len(successful_closed)
        closed_rate_pct = (successful_closed_count / trade_count * 100.0) if trade_count else 0.0

        recovery_activated_runs = [run for run in runs if bool(run.recovery_activated)]
        recovery_closed_runs = [
            run
            for run in runs
            if str(run.exit_reason or "") == "recovery_joint_exit"
        ]
        normal_closed_runs = [
            run
            for run in flat_closed
            if run.exit_reason == "flat_no_active_orders"
        ]
        recovery_failed_runs = [
            run
            for run in recovery_activated_runs
            if run not in recovery_closed_runs
        ]
        open_after_recovery_runs = [
            run
            for run in open_runs
            if bool(run.recovery_activated)
        ]
        recovery_durations = [
            int(run.recovery_duration_candles)
            for run in recovery_closed_runs
            if run.recovery_duration_candles is not None
        ]
        total_normal_pnl = sum(float(run.realized_pnl) for run in normal_closed_runs)
        total_recovery_trade_pnl = sum(float(run.realized_pnl) for run in recovery_closed_runs)

        aggregates.append(
            {
                "symbol": symbol,
                "direction": direction,
                "fill_model": fill_model,
                "config_source": config_source,
                "trades_started": trade_count,
                "closed_count": closed_count,
                "successful_closed_count": successful_closed_count,
                "undercovered_final_exit_count": len(undercovered_closed),
                "negative_pnl_closed_count": len(negative_closed),
                "unfinished_count": len(unfinished),
                "open_count": len(open_runs),
                "error_count": len(errors),
                "max_candles_count": len(max_candles_runs),
                "closed_rate_pct": closed_rate_pct,
                "successful_closed_rate_pct": closed_rate_pct,
                "total_pnl": sum(pnls),
                "avg_pnl": statistics.mean(pnls) if pnls else 0.0,
                "median_pnl": statistics.median(pnls) if pnls else 0.0,
                "best_pnl": max(pnls) if pnls else 0.0,
                "worst_pnl": min(pnls) if pnls else 0.0,
                "avg_duration_candles": statistics.mean(closed_durations) if closed_durations else 0.0,
                "total_candles_processed": total_candles,
                "first_start_time": _format_timestamp(min(start_times)) if start_times else "",
                "last_end_time": _format_timestamp(max(end_times)) if end_times else "",
                "normal_closed_count": len(normal_closed_runs),
                "recovery_activated_count": len(recovery_activated_runs),
                "recovery_closed_count": len(recovery_closed_runs),
                "recovery_failed_count": len(recovery_failed_runs),
                "open_after_recovery_count": len(open_after_recovery_runs),
                "recovery_false_positive_candidate_count": 0,
                "total_normal_pnl": total_normal_pnl,
                "total_recovery_trade_pnl": total_recovery_trade_pnl,
                "avg_recovery_duration_candles": statistics.mean(recovery_durations)
                if recovery_durations
                else 0.0,
                "max_recovery_duration_candles": max(recovery_durations) if recovery_durations else 0,
            }
        )
    return aggregates


def continuous_output_paths(
    output_dir: str | Path,
    symbol: str,
) -> tuple[Path, Path, Path]:
    base = Path(output_dir)
    symbol_upper = symbol.upper()
    summary_path = base / f"{symbol_upper}_original_hedge_5m_continuous_summary.csv"
    aggregate_path = base / f"{symbol_upper}_original_hedge_5m_continuous_aggregate.csv"
    json_path = base / f"{symbol_upper}_original_hedge_5m_continuous_results.json"
    return summary_path, aggregate_path, json_path


def write_continuous_summary_csv(path: str | Path, results: Iterable[BacktestResult]) -> Path:
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    rows = [continuous_result_to_summary_row(result) for result in results]
    with path_obj.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CONTINUOUS_SUMMARY_CSV_FIELDS))
        writer.writeheader()
        writer.writerows(rows)
    return path_obj


def write_continuous_aggregate_csv(path: str | Path, aggregates: Iterable[dict[str, Any]]) -> Path:
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    rows = list(aggregates)
    with path_obj.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CONTINUOUS_AGGREGATE_CSV_FIELDS))
        writer.writeheader()
        writer.writerows(rows)
    return path_obj


def write_continuous_results_json(
    path: str | Path,
    *,
    metadata: dict[str, Any],
    runs: Iterable[BacktestResult],
    aggregate: Iterable[dict[str, Any]],
    include_logs: bool = False,
) -> Path:
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "metadata": metadata,
        "runs": [compact_result_dict(result, include_logs=include_logs) for result in runs],
        "aggregate": list(aggregate),
    }
    with path_obj.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return path_obj


def _snapshot_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _cycle3_order_key(entry: dict[str, Any]) -> tuple[str, str] | None:
    order_id = entry.get("order_id")
    if order_id:
        return ("order_id", str(order_id))
    excerpt = entry.get("metadata_excerpt") or {}
    order_link_id = excerpt.get("order_link_id")
    if order_link_id:
        return ("order_link_id", str(order_link_id))
    return None


def _select_cycle3_fill_from_fill_log(fill_log: list[dict[str, Any]]) -> dict[str, Any] | None:
    """
    Select the final CYCLE_3_SHORT_REDUCE fill representing post-order state.

    When partial fills share an order_id or order_link_id, the last fill of that
    order group is the authoritative end state after the Cycle-3 order completes.
    """
    c3_fills = [
        entry
        for entry in fill_log
        if str(entry.get("purpose") or "") == "CYCLE_3_SHORT_REDUCE"
    ]
    if not c3_fills:
        return None

    last_fill = c3_fills[-1]
    order_key = _cycle3_order_key(last_fill)
    if order_key is None:
        return last_fill

    same_order_fills = [entry for entry in c3_fills if _cycle3_order_key(entry) == order_key]
    return same_order_fills[-1]


def _validate_cycle3_snapshot_fields(snapshot: dict[str, Any]) -> bool:
    filled_qty = snapshot.get("filled_qty")
    if filled_qty is None or filled_qty <= 0:
        return False

    fill_price = snapshot.get("fill_price")
    if fill_price is None or fill_price <= 0:
        return False

    long_qty_after = snapshot.get("long_qty_after")
    short_qty_after = snapshot.get("short_qty_after")
    if long_qty_after is None or short_qty_after is None:
        return False
    if long_qty_after < 0 or short_qty_after < 0:
        return False

    long_avg_after = snapshot.get("long_avg_after")
    short_avg_after = snapshot.get("short_avg_after")
    if long_qty_after > 0 and (long_avg_after is None or long_avg_after <= 0):
        return False
    if short_qty_after > 0 and (short_avg_after is None or short_avg_after <= 0):
        return False

    return True


def _compute_cycle3_snapshot_from_fill_log(result: BacktestResult) -> dict[str, Any] | None:
    """
    Derive a CYCLE_3_SHORT_REDUCE snapshot from BacktestResult.fill_log.

    The snapshot represents the state immediately after the confirmed final
    CYCLE_3_SHORT_REDUCE fill (including the last partial when applicable)
    and includes net/gross PnL and position state.
    """
    if not result.fill_log:
        return None

    cycle3_fill = _select_cycle3_fill_from_fill_log(result.fill_log)
    if cycle3_fill is None:
        return None

    # Compute cumulative realized net PnL up to and including the C3 fill.
    cumulative_net = 0.0
    for entry in result.fill_log:
        pnl = resolve_net_closed_pnl(entry)
        if pnl is not None:
            cumulative_net += pnl
        if entry is cycle3_fill:
            break

    local_idx = cycle3_fill.get("candle_index")
    try:
        local_idx_int = int(local_idx) if local_idx is not None else None
    except (TypeError, ValueError):
        local_idx_int = None
    start_index = result.start_index or 0
    input_slice_start = result.input_slice_start_index or 0
    slice_idx = start_index + local_idx_int if local_idx_int is not None else None
    absolute_idx = input_slice_start + slice_idx if slice_idx is not None else None

    ts = cycle3_fill.get("timestamp")
    metadata_excerpt = cycle3_fill.get("metadata_excerpt") or {}
    fee_rate = (
        metadata_excerpt.get("runtime_fee_rate")
        or metadata_excerpt.get("fee_rate")
        or None
    )
    entry_fee = metadata_excerpt.get("entry_fee")
    exit_fee = metadata_excerpt.get("exit_fee")
    gross_pnl = metadata_excerpt.get("gross_pnl")

    snapshot = {
        "purpose": "CYCLE_3_SHORT_REDUCE",
        "local_candle_index": local_idx_int,
        "slice_candle_index": slice_idx,
        "absolute_candle_index": absolute_idx,
        "input_slice_start_index": input_slice_start,
        # Backward-compatible alias for absolute feather index in new results.
        "global_candle_index": absolute_idx,
        "timestamp": ts,
        "fill_price": _snapshot_float(cycle3_fill.get("fill_price")),
        "filled_qty": _snapshot_float(cycle3_fill.get("qty")),
        "fee_rate": _snapshot_float(fee_rate),
        "entry_fee": _snapshot_float(entry_fee),
        "exit_fee": _snapshot_float(exit_fee),
        "closing_fee": (
            (_snapshot_float(entry_fee) or 0.0) + (_snapshot_float(exit_fee) or 0.0)
            if entry_fee is not None or exit_fee is not None
            else None
        ),
        "gross_realized_pnl_event": _snapshot_float(gross_pnl),
        "net_realized_pnl_event": resolve_net_closed_pnl(cycle3_fill),
        "cumulative_realized_pnl_net": cumulative_net,
        "long_qty_after": _snapshot_float(cycle3_fill.get("long_qty_after")),
        "short_qty_after": _snapshot_float(cycle3_fill.get("short_qty_after")),
        "long_avg_after": _snapshot_float(cycle3_fill.get("long_avg_after")),
        "short_avg_after": _snapshot_float(cycle3_fill.get("short_avg_after")),
    }

    # Require minimal mandatory fields for a valid snapshot.
    mandatory_keys = (
        "local_candle_index",
        "slice_candle_index",
        "absolute_candle_index",
        "timestamp",
        "fill_price",
        "filled_qty",
        "long_qty_after",
        "short_qty_after",
        "long_avg_after",
        "short_avg_after",
    )
    if any(snapshot.get(key) is None for key in mandatory_keys):
        return None
    if not _validate_cycle3_snapshot_fields(snapshot):
        return None
    return snapshot


def run_continuous_reentry_for_direction(
    symbol: str,
    direction: str,
    candles: list[Any],
    *,
    continuous_start_index: int = 0,
    continuous_max_trades: int | None = None,
    config_source: ConfigSource = "test",
    fill_model: str = "conservative",
    max_fills_per_candle: int | None = None,
    long_config_path: str | Path = DEFAULT_LONG_CONFIG_PATH,
    short_config_path: str | Path = DEFAULT_SHORT_CONFIG_PATH,
    file_config_path: str | Path | None = None,
    tp_profit_target_pct: float | None = None,
    long_fill_distance_pct: float | None = None,
    target_profit_usdt: float | None = None,
    base_notional_usdt: float | None = None,
    initial_notional_usdt: float | None = None,
    exit_rebuild_policy_config: ExitRebuildPolicyConfig | None = None,
    inventory_mtm_freeze_config: InventoryMtmFreezeConfig | None = None,
    addon_short_recovery_config: AddonShortRecoveryConfig | None = None,
    recovery_bot_config: RecoveryBotConfig | None = None,
    recovery_reentry_config: RecoveryReentryConfig | None = None,
    input_slice_start_index: int = 0,
) -> list[BacktestResult]:
    """Run chained backtests until a trade stays open or candles are exhausted."""
    symbol_upper = symbol.upper()
    signal = "short" if str(direction).lower() == "short" else "long"
    fill_config = resolve_fill_model_config(
        fill_model=fill_model,
        max_fills_per_candle=max_fills_per_candle,
    )
    candle_list = normalize_candles(symbol_upper, candles)
    if not candle_list:
        return []

    start_index = max(0, int(continuous_start_index))
    if start_index >= len(candle_list):
        return []

    results: list[BacktestResult] = []
    trade_number = 0
    recovery_state = (
        RecoveryReentryRuntimeState() if recovery_reentry_config is not None else None
    )

    while start_index < len(candle_list):
        if continuous_max_trades is not None and trade_number >= int(continuous_max_trades):
            break

        remaining = candle_list[start_index:]
        if not remaining:
            break

        trade_number += 1

        result = run_historical_backtest(
            symbol_upper,
            signal,
            remaining,
            max_candles=None,
            fill_model=fill_config.fill_model,
            max_fills_per_candle=fill_config.max_fills_per_candle,
            config_source=config_source,
            long_config_path=long_config_path,
            short_config_path=short_config_path,
            file_config_path=file_config_path,
            tp_profit_target_pct=tp_profit_target_pct,
            long_fill_distance_pct=long_fill_distance_pct,
            target_profit_usdt=target_profit_usdt,
            base_notional_usdt=base_notional_usdt,
            initial_notional_usdt=(
                float(initial_notional_usdt)
                if initial_notional_usdt is not None
                else (float(base_notional_usdt) if base_notional_usdt is not None else 100.0)
            ),
            exit_rebuild_policy_config=exit_rebuild_policy_config,
            inventory_mtm_freeze_config=inventory_mtm_freeze_config,
            addon_short_recovery_config=addon_short_recovery_config,
            recovery_bot_config=recovery_bot_config,
            absolute_trade_start_index=start_index,
            input_slice_start_index=input_slice_start_index,
        )
        result.start_index = start_index
        result.input_slice_start_index = input_slice_start_index
        result.end_index = _trade_end_index(start_index, result)
        result.trade_number = trade_number
        apply_trade_exit_quality(result)
        ensure_backtest_trade_block_ids(result)
        # Optional Cycle-3 snapshot for long-gap-reduction audits.
        result.cycle3_snapshot = _compute_cycle3_snapshot_from_fill_log(result)

        default_next_start = int(result.end_index) + 1
        recovery_should_break = False
        if recovery_reentry_config is not None and recovery_state is not None:
            outcome = apply_recovery_policy_after_trade(
                result=result,
                config=recovery_reentry_config,
                state=recovery_state,
                candle_list=candle_list,
                default_next_start_index=default_next_start,
            )
            recovery_should_break = outcome.should_break
            if not outcome.should_break and outcome.next_start_index is not None:
                default_next_start = outcome.next_start_index

        results.append(result)

        if result.exit_reason not in CONTINUOUS_SUCCESSFUL_EXIT_REASONS:
            break
        if recovery_should_break:
            break

        next_start = default_next_start
        if next_start >= len(candle_list):
            break
        start_index = next_start

    return results


def run_continuous_reentry_backtests(
    *,
    symbol: str,
    direction: str,
    candles: list[Any],
    continuous_start_index: int = 0,
    continuous_window_candles: int | None = None,
    continuous_max_trades: int | None = None,
    config_source: ConfigSource = "test",
    fill_model: str = "conservative",
    max_fills_per_candle: int | None = None,
    long_config_path: str | Path = DEFAULT_LONG_CONFIG_PATH,
    short_config_path: str | Path = DEFAULT_SHORT_CONFIG_PATH,
    file_config_path: str | Path | None = None,
    tp_profit_target_pct: float | None = None,
    long_fill_distance_pct: float | None = None,
    target_profit_usdt: float | None = None,
    base_notional_usdt: float | None = None,
    initial_notional_usdt: float | None = None,
    exit_rebuild_policy_config: ExitRebuildPolicyConfig | None = None,
    inventory_mtm_freeze_config: InventoryMtmFreezeConfig | None = None,
    addon_short_recovery_config: AddonShortRecoveryConfig | None = None,
    recovery_bot_config: RecoveryBotConfig | None = None,
    recovery_reentry_config: RecoveryReentryConfig | None = None,
    input_slice_start_index: int = 0,
    candle_source_total_count: int | None = None,
    input_slice_first_timestamp: str | None = None,
    input_slice_last_timestamp: str | None = None,
    output_dir: str | Path = "research/backtests/results",
    write_json: bool = True,
    write_csv: bool = True,
    include_logs: bool = False,
) -> dict[str, Any]:
    """Run continuous re-entry backtests for one or more directions."""
    symbol_upper = symbol.upper()
    directions = resolve_directions(direction)
    fill_config = resolve_fill_model_config(
        fill_model=fill_model,
        max_fills_per_candle=max_fills_per_candle,
    )

    candle_rows = list(candles)
    if continuous_window_candles is not None:
        candle_rows = candle_rows[: max(0, int(continuous_window_candles))]

    all_results: list[BacktestResult] = []
    for run_direction in directions:
        all_results.extend(
            run_continuous_reentry_for_direction(
                symbol_upper,
                run_direction,
                candle_rows,
                continuous_start_index=continuous_start_index,
                continuous_max_trades=continuous_max_trades,
                config_source=config_source,
                fill_model=fill_config.fill_model,
                max_fills_per_candle=fill_config.max_fills_per_candle,
                long_config_path=long_config_path,
                short_config_path=short_config_path,
                file_config_path=file_config_path,
                tp_profit_target_pct=tp_profit_target_pct,
                long_fill_distance_pct=long_fill_distance_pct,
                target_profit_usdt=target_profit_usdt,
                base_notional_usdt=base_notional_usdt,
                initial_notional_usdt=initial_notional_usdt,
                exit_rebuild_policy_config=exit_rebuild_policy_config,
                inventory_mtm_freeze_config=inventory_mtm_freeze_config,
                addon_short_recovery_config=addon_short_recovery_config,
                recovery_bot_config=recovery_bot_config,
                recovery_reentry_config=recovery_reentry_config,
                input_slice_start_index=input_slice_start_index,
            )
        )

    aggregates = aggregate_continuous_results(all_results)
    summary_path, aggregate_path, json_path = continuous_output_paths(output_dir, symbol_upper)
    written: dict[str, str | None] = {
        "summary_csv": None,
        "aggregate_csv": None,
        "json": None,
    }

    metadata = {
        "symbol": symbol_upper,
        "directions": directions,
        "continuous_reentry": True,
        "config_source": config_source,
        "fill_model": fill_config.fill_model,
        "continuous_start_index": continuous_start_index,
        "continuous_window_candles": continuous_window_candles,
        "continuous_max_trades": continuous_max_trades,
        "candles_loaded": len(candle_rows),
        "input_slice_start_index": input_slice_start_index,
        "candle_source_total_count": candle_source_total_count,
        "input_slice_first_timestamp": input_slice_first_timestamp,
        "input_slice_last_timestamp": input_slice_last_timestamp,
        "index_semantics_version": 2,
        "recovery_bot": recovery_bot_config_dict(recovery_bot_config)
        if recovery_bot_config is not None
        else None,
        "long_config_path": str(long_config_path),
        "short_config_path": str(short_config_path),
        "file_config_path": str(file_config_path) if file_config_path else None,
    }

    if write_csv:
        written["summary_csv"] = str(write_continuous_summary_csv(summary_path, all_results))
        written["aggregate_csv"] = str(write_continuous_aggregate_csv(aggregate_path, aggregates))
    if write_json:
        written["json"] = str(
            write_continuous_results_json(
                json_path,
                metadata=metadata,
                runs=all_results,
                aggregate=aggregates,
                include_logs=include_logs,
            )
        )
        metadata["output_files"] = dict(written)

    return {
        "symbol": symbol_upper,
        "directions": directions,
        "candles_loaded": len(candle_rows),
        "continuous_reentry": True,
        "config_source": config_source,
        "fill_model": fill_config.fill_model,
        "continuous_start_index": continuous_start_index,
        "continuous_window_candles": continuous_window_candles,
        "continuous_max_trades": continuous_max_trades,
        "results": all_results,
        "aggregate": aggregates,
        "output_files": written,
    }


def print_continuous_reentry_summary(payload: dict[str, Any]) -> None:
    print(
        f"symbol={payload['symbol']} candles_loaded={payload['candles_loaded']} "
        f"continuous_reentry=True config_source={payload.get('config_source')} "
        f"fill_model={payload.get('fill_model')}"
    )
    aggregates = payload.get("aggregate") or []
    for direction in payload.get("directions") or []:
        direction_rows = [row for row in aggregates if row.get("direction") == direction]
        if direction_rows:
            for row in direction_rows:
                print(
                    f"  {direction} ({row['fill_model']}): trades_started={row['trades_started']} "
                    f"closed={row['closed_count']} successful_closed={row['successful_closed_count']} "
                    f"undercovered_final_exit={row['undercovered_final_exit_count']} "
                    f"negative_pnl_closed={row['negative_pnl_closed_count']} "
                    f"open={row['open_count']} "
                    f"max_candles={row['max_candles_count']} error={row['error_count']} "
                    f"unfinished={row['unfinished_count']} "
                    f"closed_rate={row['closed_rate_pct']:.1f}% "
                    f"total_pnl={row['total_pnl']:.4f} avg_pnl={row['avg_pnl']:.4f} "
                    f"avg_duration_candles={row['avg_duration_candles']:.1f}"
                )
            continue

        runs = [result for result in payload["results"] if result.direction == direction]
        closed = sum(1 for run in runs if run.final_status == "closed")
        unfinished = sum(1 for run in runs if run.final_status != "closed")
        pnls = [float(run.realized_pnl) for run in runs]
        total_pnl = sum(pnls)
        avg_pnl = statistics.mean(pnls) if pnls else 0.0
        closed_rate = (closed / len(runs) * 100.0) if runs else 0.0
        print(
            f"  {direction}: trades_started={len(runs)} closed={closed} "
            f"unfinished={unfinished} closed_rate={closed_rate:.1f}% "
            f"total_pnl={total_pnl:.4f} avg_pnl={avg_pnl:.4f}"
        )

    output_files = payload.get("output_files") or {}
    if output_files.get("summary_csv"):
        print(f"continuous_summary_csv={output_files['summary_csv']}")
    if output_files.get("aggregate_csv"):
        print(f"continuous_aggregate_csv={output_files['aggregate_csv']}")
    if output_files.get("json"):
        print(f"continuous_json={output_files['json']}")

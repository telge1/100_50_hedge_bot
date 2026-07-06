"""Multi-start historical backtests over sliding windows (Phase 11)."""

from __future__ import annotations

import csv
import json
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .backtest_config_loader import (
    DEFAULT_LONG_CONFIG_PATH,
    DEFAULT_SHORT_CONFIG_PATH,
    ConfigSource,
)
from .backtest_report import BacktestResult
from .fill_models import FILL_MODELS, resolve_fill_model_config
from .historical_backtest import run_historical_backtest
from .recovery_bot.config import RecoveryBotConfig
from .cycle_short_tp_relief import CycleShortTpReliefConfig
from .dynamic_cycle_order_scaling import DynamicCycleOrderScalingConfig
from .stuck_recovery_reload import StuckRecoveryReloadConfig
from .trade_block_export import ensure_backtest_trade_block_ids

MULTI_START_SUMMARY_CSV_FIELDS = (
    "symbol",
    "direction",
    "start_index",
    "start_time",
    "end_time",
    "window_candles",
    "config_source",
    "config_path",
    "fill_model",
    "final_status",
    "exit_reason",
    "open_reason_detail",
    "realized_pnl",
    "fills_count",
    "cycles_seen",
    "active_orders_count",
    "final_active_order_purposes",
    "final_long_qty",
    "final_short_qty",
    "final_long_avg_price",
    "final_short_avg_price",
    "final_price",
    "unrealized_long_pnl",
    "unrealized_short_pnl",
    "unrealized_pnl",
    "overall_pnl",
    "candles_processed",
    "initial_exit_trigger",
    "price_tick_size",
    "tp_profit_target_pct",
    "base_notional_usdt",
    "hedge_ratio_short",
    # Convenience aliases for end-of-trade position/PnL
    "end_last_price",
    "end_long_qty",
    "end_long_avg_price",
    "end_short_qty",
    "end_short_avg_price",
    "end_unrealized_pnl",
    "end_overall_pnl",
)

MULTI_START_AGGREGATE_CSV_FIELDS = (
    "symbol",
    "direction",
    "fill_model",
    "config_source",
    "runs",
    "closed_count",
    "successful_closed_count",
    "open_count",
    "max_candles_count",
    "error_count",
    "unfinished_count",
    "closed_rate_pct",
    "successful_closed_rate_pct",
    "open_rate_pct",
    "max_candles_rate_pct",
    "unfinished_rate_pct",
    "total_pnl",
    "avg_pnl",
    "median_pnl",
    "best_pnl",
    "worst_pnl",
    "avg_fills_count",
    "avg_candles_processed",
    "avg_duration_candles",
    "most_common_open_reason",
    "most_common_max_candles_reason",
    "most_common_unfinished_reason",
    "most_common_final_active_order_purposes",
    "most_common_unfinished_active_order_purposes",
)

MULTI_START_UNFINISHED_CSV_FIELDS = (
    "symbol",
    "direction",
    "fill_model",
    "config_source",
    "start_index",
    "start_time",
    "end_time",
    "final_status",
    "exit_reason",
    "open_reason_detail",
    "realized_pnl",
    "fills_count",
    "cycles_seen",
    "candles_processed",
    "final_active_order_purposes",
    "final_long_qty",
    "final_short_qty",
    "final_long_avg_price",
    "final_short_avg_price",
    "final_price",
    "unrealized_long_pnl",
    "unrealized_short_pnl",
    "unrealized_pnl",
    "overall_pnl",
    "initial_exit_trigger",
    "price_tick_size",
    "tp_profit_target_pct",
)

COMPACT_RESULT_LOG_KEYS = (
    "fill_log",
    "order_log",
    "intent_log",
    "final_active_orders",
    "final_active_order_diagnostics",
    "config_diagnostics",
    "live_config_comparison",
    "exit_level_diagnostics",
)


def resolve_directions(direction: str) -> list[str]:
    normalized = str(direction or "both").strip().lower()
    if normalized == "both":
        return ["long", "short"]
    if normalized in {"long", "short"}:
        return [normalized]
    raise ValueError(f"unsupported direction: {direction}")


def generate_start_indices(
    candle_count: int,
    *,
    start_step_candles: int,
    window_candles: int,
    max_starts: int,
) -> list[int]:
    """Return start indices spaced by step, up to max_starts."""
    if candle_count < 1 or max_starts < 1 or start_step_candles < 1 or window_candles < 1:
        return []

    indices: list[int] = []
    index = 0
    while len(indices) < max_starts and index < candle_count:
        indices.append(index)
        index += start_step_candles
    return indices


def _format_timestamp(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat()


def _purposes_joined(purposes: Iterable[str] | None) -> str:
    if not purposes:
        return ""
    return "|".join(str(purpose) for purpose in purposes if purpose)


@dataclass(frozen=True)
class MultiStartRunContext:
    start_index: int
    window_candles: int


def multi_start_result_to_summary_row(result: BacktestResult) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for key in MULTI_START_SUMMARY_CSV_FIELDS:
        if key == "final_active_order_purposes":
            row[key] = _purposes_joined(result.final_active_order_purposes)
            continue
        if key == "end_last_price":
            row[key] = "" if result.final_price is None else result.final_price
            continue
        if key == "end_long_qty":
            row[key] = "" if result.final_long_qty is None else result.final_long_qty
            continue
        if key == "end_long_avg_price":
            row[key] = "" if result.final_long_avg_price is None else result.final_long_avg_price
            continue
        if key == "end_short_qty":
            row[key] = "" if result.final_short_qty is None else result.final_short_qty
            continue
        if key == "end_short_avg_price":
            row[key] = "" if result.final_short_avg_price is None else result.final_short_avg_price
            continue
        if key == "end_unrealized_pnl":
            row[key] = "" if result.unrealized_pnl is None else result.unrealized_pnl
            continue
        if key == "end_overall_pnl":
            row[key] = "" if result.overall_pnl is None else result.overall_pnl
            continue
        if key == "start_time":
            row[key] = _format_timestamp(result.start_time)
            continue
        if key == "end_time":
            row[key] = _format_timestamp(result.end_time)
            continue
        value = getattr(result, key, None)
        if value is None:
            row[key] = ""
        else:
            row[key] = value
    return row


def compact_result_dict(result: BacktestResult, *, include_logs: bool = False) -> dict[str, Any]:
    payload = result.to_dict()
    if include_logs:
        return payload
    for key in COMPACT_RESULT_LOG_KEYS:
        payload.pop(key, None)
    payload["final_active_order_purposes"] = list(result.final_active_order_purposes or [])
    return payload


def _most_common(counter: Counter[str]) -> str:
    if not counter:
        return ""
    return counter.most_common(1)[0][0]


def _run_reason(run: BacktestResult) -> str:
    return str(run.open_reason_detail or run.exit_reason or run.final_status or "").strip()


def _is_unfinished(run: BacktestResult) -> bool:
    return run.final_status != "closed"


def multi_start_result_to_unfinished_row(result: BacktestResult) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for key in MULTI_START_UNFINISHED_CSV_FIELDS:
        if key == "final_active_order_purposes":
            row[key] = _purposes_joined(result.final_active_order_purposes)
            continue
        if key in {"start_time", "end_time"}:
            row[key] = _format_timestamp(getattr(result, key, None))
            continue
        value = getattr(result, key, None)
        row[key] = "" if value is None else value
    return row


def filter_unfinished_results(results: Iterable[BacktestResult]) -> list[BacktestResult]:
    return [result for result in results if _is_unfinished(result)]


def aggregate_multi_start_results(results: Iterable[BacktestResult]) -> list[dict[str, Any]]:
    """Aggregate per-run results grouped by symbol, direction, fill_model, config_source."""
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
        run_count = len(runs)
        closed = [run for run in runs if run.final_status == "closed"]
        open_runs = [run for run in runs if run.final_status == "open"]
        max_candles_runs = [run for run in runs if run.final_status == "max_candles"]
        errors = [run for run in runs if run.final_status == "error"]
        unfinished = [run for run in runs if _is_unfinished(run)]

        pnls = [float(run.realized_pnl) for run in runs]
        fills = [int(run.fills_count) for run in runs]
        candles = [int(run.candles_processed) for run in runs]
        closed_candles = [int(run.candles_processed) for run in closed]

        open_reason_counter: Counter[str] = Counter()
        for run in open_runs:
            reason = _run_reason(run)
            if reason:
                open_reason_counter[reason] += 1

        max_candles_reason_counter: Counter[str] = Counter()
        for run in max_candles_runs:
            reason = _run_reason(run)
            if reason:
                max_candles_reason_counter[reason] += 1

        unfinished_reason_counter: Counter[str] = Counter()
        unfinished_purposes_counter: Counter[str] = Counter()
        for run in unfinished:
            reason = _run_reason(run)
            if reason:
                unfinished_reason_counter[reason] += 1
            joined = _purposes_joined(run.final_active_order_purposes)
            if joined:
                unfinished_purposes_counter[joined] += 1

        purposes_counter: Counter[str] = Counter()
        for run in runs:
            joined = _purposes_joined(run.final_active_order_purposes)
            if joined:
                purposes_counter[joined] += 1

        closed_count = len(closed)
        open_count = len(open_runs)
        max_candles_count = len(max_candles_runs)
        error_count = len(errors)
        unfinished_count = len(unfinished)
        closed_rate_pct = (closed_count / run_count * 100.0) if run_count else 0.0
        aggregates.append(
            {
                "symbol": symbol,
                "direction": direction,
                "fill_model": fill_model,
                "config_source": config_source,
                "runs": run_count,
                "closed_count": closed_count,
                "successful_closed_count": closed_count,
                "open_count": open_count,
                "max_candles_count": max_candles_count,
                "error_count": error_count,
                "unfinished_count": unfinished_count,
                "closed_rate_pct": closed_rate_pct,
                "successful_closed_rate_pct": closed_rate_pct,
                "open_rate_pct": (open_count / run_count * 100.0) if run_count else 0.0,
                "max_candles_rate_pct": (max_candles_count / run_count * 100.0) if run_count else 0.0,
                "unfinished_rate_pct": (unfinished_count / run_count * 100.0) if run_count else 0.0,
                "total_pnl": sum(pnls),
                "avg_pnl": statistics.mean(pnls) if pnls else 0.0,
                "median_pnl": statistics.median(pnls) if pnls else 0.0,
                "best_pnl": max(pnls) if pnls else 0.0,
                "worst_pnl": min(pnls) if pnls else 0.0,
                "avg_fills_count": statistics.mean(fills) if fills else 0.0,
                "avg_candles_processed": statistics.mean(candles) if candles else 0.0,
                "avg_duration_candles": statistics.mean(closed_candles) if closed_candles else 0.0,
                "most_common_open_reason": _most_common(open_reason_counter),
                "most_common_max_candles_reason": _most_common(max_candles_reason_counter),
                "most_common_unfinished_reason": _most_common(unfinished_reason_counter),
                "most_common_final_active_order_purposes": _most_common(purposes_counter),
                "most_common_unfinished_active_order_purposes": _most_common(
                    unfinished_purposes_counter
                ),
            }
        )
    return aggregates


def multi_start_output_paths(
    output_dir: str | Path,
    symbol: str,
) -> tuple[Path, Path, Path, Path]:
    base = Path(output_dir)
    symbol_upper = symbol.upper()
    summary_path = base / f"{symbol_upper}_original_hedge_5m_multi_start_summary.csv"
    json_path = base / f"{symbol_upper}_original_hedge_5m_multi_start_results.json"
    aggregate_path = base / f"{symbol_upper}_original_hedge_5m_multi_start_aggregate.csv"
    unfinished_path = base / f"{symbol_upper}_original_hedge_5m_multi_start_unfinished.csv"
    return summary_path, json_path, aggregate_path, unfinished_path


def write_multi_start_summary_csv(path: str | Path, results: Iterable[BacktestResult]) -> Path:
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    rows = [multi_start_result_to_summary_row(result) for result in results]
    with path_obj.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(MULTI_START_SUMMARY_CSV_FIELDS))
        writer.writeheader()
        writer.writerows(rows)
    return path_obj


def write_multi_start_unfinished_csv(path: str | Path, results: Iterable[BacktestResult]) -> Path:
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    unfinished = filter_unfinished_results(results)
    rows = [multi_start_result_to_unfinished_row(result) for result in unfinished]
    with path_obj.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(MULTI_START_UNFINISHED_CSV_FIELDS))
        writer.writeheader()
        writer.writerows(rows)
    return path_obj


def write_multi_start_aggregate_csv(path: str | Path, aggregates: Iterable[dict[str, Any]]) -> Path:
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    rows = list(aggregates)
    with path_obj.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(MULTI_START_AGGREGATE_CSV_FIELDS))
        writer.writeheader()
        writer.writerows(rows)
    return path_obj


def write_multi_start_results_json(
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


def run_multi_start_backtest(
    symbol: str,
    direction: str,
    candles: list[Any],
    *,
    config_source: ConfigSource = "live",
    fill_model: str = "conservative",
    max_fills_per_candle: int | None = None,
    start_step_candles: int = 100,
    window_candles: int = 1000,
    max_starts: int = 20,
    long_config_path: str | Path = DEFAULT_LONG_CONFIG_PATH,
    short_config_path: str | Path = DEFAULT_SHORT_CONFIG_PATH,
    file_config_path: str | Path | None = None,
    tp_profit_target_pct: float | None = None,
    dynamic_cycle_scaling_config: DynamicCycleOrderScalingConfig | None = None,
    stuck_recovery_reload_config: StuckRecoveryReloadConfig | None = None,
    cycle_short_tp_relief_config: CycleShortTpReliefConfig | None = None,
    use_live_short_tp_relief: bool = False,
    recovery_bot_config: RecoveryBotConfig | None = None,
) -> list[BacktestResult]:
    """Run multiple backtests at staggered start points over the candle series."""
    symbol_upper = symbol.upper()
    signal = "short" if str(direction).lower() == "short" else "long"
    fill_config = resolve_fill_model_config(
        fill_model=fill_model,
        max_fills_per_candle=max_fills_per_candle,
    )
    start_indices = generate_start_indices(
        len(candles),
        start_step_candles=start_step_candles,
        window_candles=window_candles,
        max_starts=max_starts,
    )

    results: list[BacktestResult] = []
    effective_cycle_short_tp_relief_config: CycleShortTpReliefConfig | None
    if use_live_short_tp_relief:
        # Live-Relief-Pfad: Shim-Konfiguration darf nicht installiert werden.
        effective_cycle_short_tp_relief_config = None
    else:
        effective_cycle_short_tp_relief_config = cycle_short_tp_relief_config
    max_loop_candles = max(0, window_candles - 1)
    for start_index in start_indices:
        window = candles[start_index : start_index + window_candles]
        if not window:
            continue
        result = run_historical_backtest(
            symbol_upper,
            signal,
            window,
            max_candles=max_loop_candles,
            fill_model=fill_config.fill_model,
            max_fills_per_candle=fill_config.max_fills_per_candle,
            config_source=config_source,
            long_config_path=long_config_path,
            short_config_path=short_config_path,
            file_config_path=file_config_path,
            tp_profit_target_pct=tp_profit_target_pct,
            dynamic_cycle_scaling_config=dynamic_cycle_scaling_config,
            stuck_recovery_reload_config=stuck_recovery_reload_config,
            cycle_short_tp_relief_config=effective_cycle_short_tp_relief_config,
            use_live_short_tp_relief=use_live_short_tp_relief,
            recovery_bot_config=recovery_bot_config,
        )
        result.start_index = start_index
        result.window_candles = min(window_candles, len(window))
        ensure_backtest_trade_block_ids(result)
        results.append(result)
    return results


def resolve_multi_start_fill_models(
    *,
    fill_model: str,
    multi_fill_models: bool,
) -> list[str]:
    if multi_fill_models:
        return list(FILL_MODELS)
    return [resolve_fill_model_config(fill_model=fill_model).fill_model]


def run_multi_start_backtests(
    *,
    symbol: str,
    direction: str,
    candles: list[Any],
    config_source: ConfigSource = "live",
    fill_model: str = "conservative",
    max_fills_per_candle: int | None = None,
    multi_fill_models: bool = False,
    start_step_candles: int = 100,
    window_candles: int = 1000,
    max_starts: int = 20,
    long_config_path: str | Path = DEFAULT_LONG_CONFIG_PATH,
    short_config_path: str | Path = DEFAULT_SHORT_CONFIG_PATH,
    file_config_path: str | Path | None = None,
    tp_profit_target_pct: float | None = None,
    output_dir: str | Path = "research/backtests/results",
    write_json: bool = True,
    write_csv: bool = True,
    include_logs: bool = False,
    dynamic_cycle_scaling_config: DynamicCycleOrderScalingConfig | None = None,
    stuck_recovery_reload_config: StuckRecoveryReloadConfig | None = None,
    cycle_short_tp_relief_config: CycleShortTpReliefConfig | None = None,
    use_live_short_tp_relief: bool = False,
    recovery_bot_config: RecoveryBotConfig | None = None,
) -> dict[str, Any]:
    """Run multi-start backtests for one or more directions and fill models."""
    symbol_upper = symbol.upper()
    directions = resolve_directions(direction)
    fill_models = resolve_multi_start_fill_models(
        fill_model=fill_model,
        multi_fill_models=multi_fill_models,
    )

    all_results: list[BacktestResult] = []
    for model in fill_models:
        for run_direction in directions:
            all_results.extend(
                run_multi_start_backtest(
                    symbol_upper,
                    run_direction,
                    candles,
                    config_source=config_source,
                    fill_model=model,
                    max_fills_per_candle=max_fills_per_candle,
                    start_step_candles=start_step_candles,
                    window_candles=window_candles,
                    max_starts=max_starts,
                    long_config_path=long_config_path,
                    short_config_path=short_config_path,
                    file_config_path=file_config_path,
                    tp_profit_target_pct=tp_profit_target_pct,
                    dynamic_cycle_scaling_config=dynamic_cycle_scaling_config,
                    stuck_recovery_reload_config=stuck_recovery_reload_config,
                    cycle_short_tp_relief_config=cycle_short_tp_relief_config,
                    use_live_short_tp_relief=use_live_short_tp_relief,
                    recovery_bot_config=recovery_bot_config,
                )
            )

    aggregates = aggregate_multi_start_results(all_results)
    summary_path, json_path, aggregate_path, unfinished_path = multi_start_output_paths(
        output_dir,
        symbol_upper,
    )
    written: dict[str, str | None] = {
        "summary_csv": None,
        "json": None,
        "aggregate_csv": None,
        "unfinished_csv": None,
    }

    metadata = {
        "symbol": symbol_upper,
        "directions": directions,
        "config_source": config_source,
        "fill_models": fill_models,
        "fill_model": fill_model if not multi_fill_models else None,
        "multi_fill_models": multi_fill_models,
        "start_step_candles": start_step_candles,
        "window_candles": window_candles,
        "max_starts": max_starts,
        "candles_loaded": len(candles),
        "long_config_path": str(long_config_path),
        "short_config_path": str(short_config_path),
        "file_config_path": str(file_config_path) if file_config_path else None,
    }

    if write_csv:
        written["summary_csv"] = str(write_multi_start_summary_csv(summary_path, all_results))
        written["aggregate_csv"] = str(write_multi_start_aggregate_csv(aggregate_path, aggregates))
        written["unfinished_csv"] = str(write_multi_start_unfinished_csv(unfinished_path, all_results))
    if write_json:
        written["json"] = str(json_path)
        metadata["output_files"] = dict(written)
        written["json"] = str(
            write_multi_start_results_json(
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
        "candles_loaded": len(candles),
        "multi_start": True,
        "config_source": config_source,
        "fill_models": fill_models,
        "start_step_candles": start_step_candles,
        "window_candles": window_candles,
        "max_starts": max_starts,
        "results": all_results,
        "aggregate": aggregates,
        "output_files": written,
    }


def print_multi_start_summary(payload: dict[str, Any]) -> None:
    fill_models = payload.get("fill_models") or [payload.get("fill_model")]
    fill_label = ",".join(str(model) for model in fill_models if model)
    print(
        f"symbol={payload['symbol']} candles_loaded={payload['candles_loaded']} "
        f"multi_start=True config_source={payload.get('config_source')} "
        f"fill_model={fill_label}"
    )
    aggregates = payload.get("aggregate") or []
    for direction in payload.get("directions") or []:
        direction_rows = [row for row in aggregates if row.get("direction") == direction]
        if not direction_rows:
            runs = [r for r in payload["results"] if r.direction == direction]
            closed = sum(1 for r in runs if r.final_status == "closed")
            open_count = sum(1 for r in runs if r.final_status == "open")
            max_candles = sum(1 for r in runs if r.final_status == "max_candles")
            error_count = sum(1 for r in runs if r.final_status == "error")
            unfinished = open_count + max_candles + error_count
            closed_rate = (closed / len(runs) * 100.0) if runs else 0.0
            pnls = [float(r.realized_pnl) for r in runs]
            total_pnl = sum(pnls)
            avg_pnl = statistics.mean(pnls) if pnls else 0.0
            print(
                f"  {direction}: runs={len(runs)} closed={closed} max_candles={max_candles} "
                f"open={open_count} error={error_count} unfinished={unfinished} "
                f"closed_rate={closed_rate:.1f}% total_pnl={total_pnl:.4f} avg_pnl={avg_pnl:.4f}"
            )
            continue
        for row in direction_rows:
            print(
                f"  {direction} ({row['fill_model']}): runs={row['runs']} "
                f"closed={row['closed_count']} max_candles={row['max_candles_count']} "
                f"open={row['open_count']} error={row['error_count']} "
                f"unfinished={row['unfinished_count']} "
                f"closed_rate={row['closed_rate_pct']:.1f}% "
                f"unfinished_rate={row['unfinished_rate_pct']:.1f}% "
                f"total_pnl={row['total_pnl']:.4f} avg_pnl={row['avg_pnl']:.4f}"
            )
    output_files = payload.get("output_files") or {}
    if output_files.get("summary_csv"):
        print(f"summary_csv={output_files['summary_csv']}")
    if output_files.get("aggregate_csv"):
        print(f"aggregate_csv={output_files['aggregate_csv']}")
    if output_files.get("unfinished_csv"):
        print(f"unfinished_csv={output_files['unfinished_csv']}")
    if output_files.get("json"):
        print(f"json={output_files['json']}")

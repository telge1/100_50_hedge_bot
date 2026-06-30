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
from .historical_backtest import normalize_candles, run_historical_backtest
from .pnl_coverage_audit import apply_trade_exit_quality
from .multi_start_backtest import compact_result_dict, resolve_directions

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
)


def continuous_trade_block_id(direction: str, trade_number: int) -> str:
    return f"backtest_{direction}_continuous_trade_{trade_number:04d}"


def stamp_trade_block_id(result: BacktestResult, trade_block_id: str) -> None:
    result.trade_block_id = trade_block_id
    for log in (result.fill_log, result.order_log, result.intent_log):
        for record in log or []:
            record.setdefault("trade_block_id", trade_block_id)
    for order in result.final_active_orders or []:
        order.setdefault("trade_block_id", trade_block_id)
    excerpt = dict(result.final_strategy_state_excerpt or {})
    excerpt["active_trade_block_id"] = trade_block_id
    result.final_strategy_state_excerpt = excerpt


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

    while start_index < len(candle_list):
        if continuous_max_trades is not None and trade_number >= int(continuous_max_trades):
            break

        remaining = candle_list[start_index:]
        if not remaining:
            break

        trade_number += 1
        block_id = continuous_trade_block_id(signal, trade_number)

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
        )
        result.start_index = start_index
        result.end_index = _trade_end_index(start_index, result)
        result.trade_number = trade_number
        apply_trade_exit_quality(result)
        stamp_trade_block_id(result, block_id)
        results.append(result)

        if result.exit_reason != "flat_no_active_orders":
            break

        next_start = int(result.end_index) + 1
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

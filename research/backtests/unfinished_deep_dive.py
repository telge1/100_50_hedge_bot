"""Deep-dive analysis for unfinished multi-start backtest runs (Phase 13)."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
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
from .historical_backtest import run_historical_backtest
from .intent_diagnostics import summarize_exit_diagnostics
from .multi_start_backtest import filter_unfinished_results
from .recovery_bot.config import RecoveryBotConfig

DEEP_DIVE_CSV_FIELDS = (
    "symbol",
    "direction",
    "start_index",
    "start_time",
    "original_window_candles",
    "extended_window_candles",
    "original_final_status",
    "original_exit_reason",
    "original_open_reason_detail",
    "original_realized_pnl",
    "original_fills_count",
    "original_candles_processed",
    "original_final_active_order_purposes",
    "original_final_long_qty",
    "original_final_short_qty",
    "extended_final_status",
    "extended_exit_reason",
    "extended_open_reason_detail",
    "extended_realized_pnl",
    "extended_fills_count",
    "extended_candles_processed",
    "extended_final_active_order_purposes",
    "extended_final_long_qty",
    "extended_final_short_qty",
    "resolved_with_extended_window",
    "additional_candles_needed",
    "additional_pnl",
    "additional_fills",
    "still_unfinished_reason",
    "still_unfinished_active_order_purposes",
    "original_max_high_after_active_orders",
    "original_min_low_after_active_orders",
    "extended_max_high_after_active_orders",
    "extended_min_low_after_active_orders",
    "final_active_order_diagnostics_summary",
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


def parse_deep_dive_start_indices(text: str | None) -> set[int] | None:
    if not text or not str(text).strip():
        return None
    indices: set[int] = set()
    for part in str(text).split(","):
        part = part.strip()
        if not part:
            continue
        indices.add(int(part))
    return indices or None


def _format_timestamp(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat()


def _purposes_joined(purposes: Iterable[str] | None) -> str:
    if not purposes:
        return ""
    return "|".join(str(purpose) for purpose in purposes if purpose)


def _price_extremes_from_diagnostics(
    diagnostics: list[dict[str, Any]] | None,
) -> tuple[float | None, float | None]:
    if not diagnostics:
        return None, None
    max_highs = [
        float(item["max_high_after_created"])
        for item in diagnostics
        if item.get("max_high_after_created") is not None
    ]
    min_lows = [
        float(item["min_low_after_created"])
        for item in diagnostics
        if item.get("min_low_after_created") is not None
    ]
    return (
        max(max_highs) if max_highs else None,
        min(min_lows) if min_lows else None,
    )


def _run_backtest_window(
    *,
    symbol: str,
    direction: str,
    candles: list[Any],
    start_index: int,
    window_candles: int,
    config_source: ConfigSource,
    fill_model: str,
    max_fills_per_candle: int | None,
    long_config_path: str | Path,
    short_config_path: str | Path,
    file_config_path: str | Path | None,
    recovery_bot_config: RecoveryBotConfig | None = None,
) -> BacktestResult:
    fill_config = resolve_fill_model_config(
        fill_model=fill_model,
        max_fills_per_candle=max_fills_per_candle,
    )
    window = candles[start_index : start_index + window_candles]
    if not window:
        raise ValueError(f"empty candle window at start_index={start_index}")
    result = run_historical_backtest(
        symbol,
        direction,
        window,
        max_candles=max(0, window_candles - 1),
        fill_model=fill_config.fill_model,
        max_fills_per_candle=fill_config.max_fills_per_candle,
        config_source=config_source,
        long_config_path=long_config_path,
        short_config_path=short_config_path,
        file_config_path=file_config_path,
        recovery_bot_config=recovery_bot_config,
    )
    result.start_index = start_index
    result.window_candles = min(window_candles, len(window))
    return result


def build_deep_dive_comparison_row(
    *,
    original: BacktestResult,
    extended: BacktestResult,
    original_window_candles: int,
    extended_window_candles: int,
) -> dict[str, Any]:
    resolved = extended.final_status == "closed"
    original_pnl = float(original.realized_pnl)
    extended_pnl = float(extended.realized_pnl)
    original_fills = int(original.fills_count)
    extended_fills = int(extended.fills_count)
    original_processed = int(original.candles_processed)
    extended_processed = int(extended.candles_processed)

    orig_max_high, orig_min_low = _price_extremes_from_diagnostics(
        original.final_active_order_diagnostics
    )
    ext_max_high, ext_min_low = _price_extremes_from_diagnostics(
        extended.final_active_order_diagnostics
    )

    row: dict[str, Any] = {
        "symbol": original.symbol,
        "direction": original.direction,
        "start_index": original.start_index,
        "start_time": _format_timestamp(original.start_time),
        "original_window_candles": original_window_candles,
        "extended_window_candles": extended_window_candles,
        "original_final_status": original.final_status,
        "original_exit_reason": original.exit_reason,
        "original_open_reason_detail": original.open_reason_detail,
        "original_realized_pnl": original_pnl,
        "original_fills_count": original_fills,
        "original_candles_processed": original_processed,
        "original_final_active_order_purposes": _purposes_joined(
            original.final_active_order_purposes
        ),
        "original_final_long_qty": original.final_long_qty,
        "original_final_short_qty": original.final_short_qty,
        "extended_final_status": extended.final_status,
        "extended_exit_reason": extended.exit_reason,
        "extended_open_reason_detail": extended.open_reason_detail,
        "extended_realized_pnl": extended_pnl,
        "extended_fills_count": extended_fills,
        "extended_candles_processed": extended_processed,
        "extended_final_active_order_purposes": _purposes_joined(
            extended.final_active_order_purposes
        ),
        "extended_final_long_qty": extended.final_long_qty,
        "extended_final_short_qty": extended.final_short_qty,
        "resolved_with_extended_window": resolved,
        "additional_candles_needed": (
            extended_processed - original_processed if resolved else ""
        ),
        "additional_pnl": extended_pnl - original_pnl,
        "additional_fills": extended_fills - original_fills,
        "still_unfinished_reason": (
            ""
            if resolved
            else str(extended.open_reason_detail or extended.exit_reason or extended.final_status)
        ),
        "still_unfinished_active_order_purposes": (
            ""
            if resolved
            else _purposes_joined(extended.final_active_order_purposes)
        ),
        "original_max_high_after_active_orders": orig_max_high,
        "original_min_low_after_active_orders": orig_min_low,
        "extended_max_high_after_active_orders": ext_max_high,
        "extended_min_low_after_active_orders": ext_min_low,
        "final_active_order_diagnostics_summary": summarize_exit_diagnostics(
            original.final_active_order_diagnostics or []
        ),
    }
    return row


def result_to_deep_dive_summary(result: BacktestResult, *, include_logs: bool = False) -> dict[str, Any]:
    payload = asdict(result)
    if result.start_time is not None:
        payload["start_time"] = result.start_time.isoformat()
    if result.end_time is not None:
        payload["end_time"] = result.end_time.isoformat()
    payload["final_active_order_purposes"] = list(result.final_active_order_purposes or [])
    payload["final_strategy_state_excerpt"] = dict(result.final_strategy_state_excerpt or {})
    if not include_logs:
        for key in COMPACT_RESULT_LOG_KEYS:
            payload.pop(key, None)
    return payload


def select_unfinished_runs_for_deep_dive(
    results: Iterable[BacktestResult],
    *,
    start_indices: set[int] | None = None,
) -> list[BacktestResult]:
    unfinished = filter_unfinished_results(results)
    if start_indices is None:
        return unfinished
    return [run for run in unfinished if run.start_index in start_indices]


def run_unfinished_deep_dive(
    *,
    symbol: str,
    candles: list[Any],
    unfinished_runs: Iterable[BacktestResult],
    config_source: ConfigSource = "live",
    fill_model: str = "conservative",
    max_fills_per_candle: int | None = None,
    original_window_candles: int = 1000,
    extended_window_candles: int = 3000,
    long_config_path: str | Path = DEFAULT_LONG_CONFIG_PATH,
    short_config_path: str | Path = DEFAULT_SHORT_CONFIG_PATH,
    file_config_path: str | Path | None = None,
    recovery_bot_config: RecoveryBotConfig | None = None,
) -> list[dict[str, Any]]:
    """Re-run unfinished multi-start windows with an extended candle horizon."""
    symbol_upper = symbol.upper()
    rows: list[dict[str, Any]] = []
    for original in unfinished_runs:
        if original.start_index is None:
            continue
        start_index = int(original.start_index)
        if start_index + extended_window_candles > len(candles):
            continue
        extended = _run_backtest_window(
            symbol=symbol_upper,
            direction=original.direction,
            candles=candles,
            start_index=start_index,
            window_candles=extended_window_candles,
            config_source=config_source,
            fill_model=fill_model,
            max_fills_per_candle=max_fills_per_candle,
            long_config_path=long_config_path,
            short_config_path=short_config_path,
            file_config_path=file_config_path,
            recovery_bot_config=recovery_bot_config,
        )
        rows.append(
            build_deep_dive_comparison_row(
                original=original,
                extended=extended,
                original_window_candles=original_window_candles,
                extended_window_candles=extended_window_candles,
            )
        )
    return rows


def deep_dive_output_paths(output_dir: str | Path, symbol: str) -> tuple[Path, Path]:
    base = Path(output_dir)
    symbol_upper = symbol.upper()
    csv_path = base / f"{symbol_upper}_original_hedge_5m_unfinished_deep_dive.csv"
    json_path = base / f"{symbol_upper}_original_hedge_5m_unfinished_deep_dive_results.json"
    return csv_path, json_path


def write_unfinished_deep_dive_csv(path: str | Path, rows: Iterable[dict[str, Any]]) -> Path:
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    row_list = list(rows)
    with path_obj.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(DEEP_DIVE_CSV_FIELDS))
        writer.writeheader()
        for row in row_list:
            csv_row = {key: ("" if row.get(key) is None else row.get(key)) for key in DEEP_DIVE_CSV_FIELDS}
            writer.writerow(csv_row)
    return path_obj


def write_unfinished_deep_dive_json(
    path: str | Path,
    *,
    metadata: dict[str, Any],
    rows: list[dict[str, Any]],
    original_results: list[BacktestResult],
    extended_results: list[BacktestResult],
    include_logs: bool = False,
) -> Path:
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    deep_dive_runs: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        original = original_results[index] if index < len(original_results) else None
        extended = extended_results[index] if index < len(extended_results) else None
        entry: dict[str, Any] = {
            "comparison": row,
            "original_result": (
                result_to_deep_dive_summary(original, include_logs=include_logs)
                if original is not None
                else None
            ),
            "extended_result": (
                result_to_deep_dive_summary(extended, include_logs=include_logs)
                if extended is not None
                else None
            ),
        }
        if original is not None:
            entry["original_diagnostics"] = {
                "final_active_order_diagnostics": original.final_active_order_diagnostics,
                "strategy_state_excerpt": original.final_strategy_state_excerpt,
            }
        if extended is not None:
            entry["extended_diagnostics"] = {
                "final_active_order_diagnostics": extended.final_active_order_diagnostics,
                "strategy_state_excerpt": extended.final_strategy_state_excerpt,
            }
        deep_dive_runs.append(entry)

    payload = {
        "metadata": metadata,
        "deep_dive_runs": deep_dive_runs,
    }
    with path_obj.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return path_obj


def run_unfinished_deep_dive_after_multi_start(
    *,
    multi_start_payload: dict[str, Any],
    candles: list[Any],
    config_source: ConfigSource,
    fill_model: str,
    max_fills_per_candle: int | None,
    original_window_candles: int,
    extended_window_candles: int,
    deep_dive_start_indices: set[int] | None = None,
    long_config_path: str | Path = DEFAULT_LONG_CONFIG_PATH,
    short_config_path: str | Path = DEFAULT_SHORT_CONFIG_PATH,
    file_config_path: str | Path | None = None,
    output_dir: str | Path = "research/backtests/results",
    write_json: bool = True,
    write_csv: bool = True,
    include_logs: bool = False,
    recovery_bot_config: RecoveryBotConfig | None = None,
) -> dict[str, Any]:
    """Run deep-dive comparisons for unfinished runs from a multi-start payload."""
    symbol_upper = str(multi_start_payload["symbol"]).upper()
    unfinished = select_unfinished_runs_for_deep_dive(
        multi_start_payload.get("results") or [],
        start_indices=deep_dive_start_indices,
    )
    unfinished = sorted(
        unfinished,
        key=lambda run: (run.direction, run.start_index if run.start_index is not None else -1),
    )

    original_results = list(unfinished)
    extended_results: list[BacktestResult] = []
    rows: list[dict[str, Any]] = []

    for original in unfinished:
        if original.start_index is None:
            continue
        start_index = int(original.start_index)
        if start_index + extended_window_candles > len(candles):
            continue
        extended = _run_backtest_window(
            symbol=symbol_upper,
            direction=original.direction,
            candles=candles,
            start_index=start_index,
            window_candles=extended_window_candles,
            config_source=config_source,
            fill_model=fill_model,
            max_fills_per_candle=max_fills_per_candle,
            long_config_path=long_config_path,
            short_config_path=short_config_path,
            file_config_path=file_config_path,
            recovery_bot_config=recovery_bot_config,
        )
        extended_results.append(extended)
        rows.append(
            build_deep_dive_comparison_row(
                original=original,
                extended=extended,
                original_window_candles=original_window_candles,
                extended_window_candles=extended_window_candles,
            )
        )

    csv_path, json_path = deep_dive_output_paths(output_dir, symbol_upper)
    written: dict[str, str | None] = {"deep_dive_csv": None, "deep_dive_json": None}

    metadata = {
        "symbol": symbol_upper,
        "config_source": config_source,
        "fill_model": fill_model,
        "original_window_candles": original_window_candles,
        "extended_window_candles": extended_window_candles,
        "start_indices": sorted(
            {
                int(run.start_index)
                for run in original_results
                if run.start_index is not None
            }
        ),
        "directions": multi_start_payload.get("directions") or [],
        "original_multi_start_aggregate": multi_start_payload.get("aggregate") or [],
        "deep_dive_start_indices_filter": (
            sorted(deep_dive_start_indices) if deep_dive_start_indices else None
        ),
    }

    if write_csv:
        written["deep_dive_csv"] = str(write_unfinished_deep_dive_csv(csv_path, rows))
    if write_json:
        metadata["output_files"] = dict(written)
        written["deep_dive_json"] = str(
            write_unfinished_deep_dive_json(
                json_path,
                metadata=metadata,
                rows=rows,
                original_results=original_results[: len(rows)],
                extended_results=extended_results,
                include_logs=include_logs,
            )
        )
        metadata["output_files"] = dict(written)

    resolved_count = sum(1 for row in rows if row.get("resolved_with_extended_window"))
    return {
        "symbol": symbol_upper,
        "rows": rows,
        "original_results": original_results[: len(rows)],
        "extended_results": extended_results,
        "resolved_count": resolved_count,
        "still_unfinished_count": len(rows) - resolved_count,
        "metadata": metadata,
        "output_files": written,
    }


def format_deep_dive_console_line(row: dict[str, Any]) -> str:
    direction = row["direction"]
    start = row["start_index"]
    orig_status = row["original_final_status"]
    orig_pnl = float(row["original_realized_pnl"])
    ext_pnl = float(row["extended_realized_pnl"])
    if row.get("resolved_with_extended_window"):
        additional = row.get("additional_candles_needed")
        return (
            f"{direction} start={start}: {orig_status} -> closed after +{additional} candles "
            f"pnl {orig_pnl:+.2f} -> {ext_pnl:+.2f}"
        )
    active = row.get("still_unfinished_active_order_purposes") or row.get(
        "extended_final_active_order_purposes"
    )
    ext_status = row.get("extended_final_status")
    return f"{direction} start={start}: {orig_status} -> still {ext_status} active={active}"


def print_unfinished_deep_dive_summary(payload: dict[str, Any]) -> None:
    rows = payload.get("rows") or []
    if not rows:
        print("Deep dive unfinished: no unfinished runs selected")
        return
    print("Deep dive unfinished:")
    for row in rows:
        print(f"  {format_deep_dive_console_line(row)}")
    resolved = int(payload.get("resolved_count") or 0)
    total = len(rows)
    still = int(payload.get("still_unfinished_count") or (total - resolved))
    print(f"Aggregate: resolved={resolved}/{total} still_unfinished={still}/{total}")
    output_files = payload.get("output_files") or {}
    if output_files.get("deep_dive_csv"):
        print(f"deep_dive_csv={output_files['deep_dive_csv']}")
    if output_files.get("deep_dive_json"):
        print(f"deep_dive_json={output_files['deep_dive_json']}")

"""CLI runner for original hedge bot historical backtests (Phase 5/7)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .backtest_report import (
    BacktestResult,
    comparison_output_paths,
    default_output_paths,
    write_results_json,
    write_summary_csv,
)
from .candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol
from .debug_report import print_debug_report
from .fill_models import COMPARE_FILL_MODELS, resolve_fill_model_config
from .historical_backtest import run_historical_backtest


def resolve_directions(direction: str) -> list[str]:
    normalized = str(direction or "both").strip().lower()
    if normalized == "both":
        return ["long", "short"]
    if normalized in {"long", "short"}:
        return [normalized]
    raise ValueError(f"unsupported direction: {direction}")


def _load_candles(
    *,
    symbol: str,
    limit: int,
    data_dir: str | Path,
) -> list[dict[str, Any]]:
    candles = load_candles_for_symbol(
        symbol,
        timeframe="5m",
        data_dir=data_dir,
        limit=limit,
    )
    if not candles:
        raise FileNotFoundError(f"no candles loaded for {symbol.upper()} under {data_dir}")
    return candles


def run_original_hedge_backtests(
    *,
    symbol: str = "APTUSDT",
    direction: str = "both",
    limit: int = 1000,
    max_candles: int | None = None,
    data_dir: str | Path = DEFAULT_DATA_DIR,
    output_dir: str | Path = "research/backtests/results",
    fill_model: str = "conservative",
    max_fills_per_candle: int | None = None,
    write_json: bool = True,
    write_csv: bool = True,
    candles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    symbol_upper = symbol.upper()
    directions = resolve_directions(direction)
    effective_max_candles = max_candles if max_candles is not None else limit
    fill_config = resolve_fill_model_config(
        fill_model=fill_model,
        max_fills_per_candle=max_fills_per_candle,
    )

    candle_rows = candles
    if candle_rows is None:
        candle_rows = _load_candles(symbol=symbol_upper, limit=limit, data_dir=data_dir)

    results: dict[str, BacktestResult] = {}
    for run_direction in directions:
        results[run_direction] = run_historical_backtest(
            symbol_upper,
            run_direction,
            candle_rows,
            max_candles=effective_max_candles,
            fill_model=fill_config.fill_model,
            max_fills_per_candle=fill_config.max_fills_per_candle,
        )

    json_path, csv_path = default_output_paths(output_dir, symbol_upper)
    written: dict[str, str | None] = {"json": None, "csv": None}

    if write_json:
        written["json"] = str(
            write_results_json(
                json_path,
                symbol=symbol_upper,
                limit=limit,
                max_candles=effective_max_candles,
                results=results,
                meta={
                    "data_dir": str(data_dir),
                    "fill_model": fill_config.fill_model,
                    "max_fills_per_candle": fill_config.max_fills_per_candle,
                    "directions": directions,
                },
            )
        )
    if write_csv:
        written["csv"] = str(write_summary_csv(csv_path, results.values()))

    return {
        "symbol": symbol_upper,
        "directions": directions,
        "limit": limit,
        "max_candles": effective_max_candles,
        "candles_loaded": len(candle_rows),
        "fill_model": fill_config.fill_model,
        "max_fills_per_candle": fill_config.max_fills_per_candle,
        "results": results,
        "output_files": written,
    }


def run_fill_model_comparison(
    *,
    symbol: str = "APTUSDT",
    direction: str = "both",
    limit: int = 1000,
    max_candles: int | None = None,
    data_dir: str | Path = DEFAULT_DATA_DIR,
    output_dir: str | Path = "research/backtests/results",
    write_json: bool = True,
    write_csv: bool = True,
) -> dict[str, Any]:
    symbol_upper = symbol.upper()
    directions = resolve_directions(direction)
    effective_max_candles = max_candles if max_candles is not None else limit
    candle_rows = _load_candles(symbol=symbol_upper, limit=limit, data_dir=data_dir)

    results: dict[str, BacktestResult] = {}
    for model_name, explicit_max in COMPARE_FILL_MODELS:
        fill_config = resolve_fill_model_config(
            fill_model=model_name,
            max_fills_per_candle=explicit_max,
        )
        for run_direction in directions:
            key = f"{run_direction}:{fill_config.fill_model}"
            results[key] = run_historical_backtest(
                symbol_upper,
                run_direction,
                candle_rows,
                max_candles=effective_max_candles,
                fill_model=fill_config.fill_model,
                max_fills_per_candle=fill_config.max_fills_per_candle,
            )

    json_path, csv_path = comparison_output_paths(output_dir, symbol_upper)
    written: dict[str, str | None] = {"json": None, "csv": None}
    if write_json:
        written["json"] = str(
            write_results_json(
                json_path,
                symbol=symbol_upper,
                limit=limit,
                max_candles=effective_max_candles,
                results=results,
                meta={
                    "data_dir": str(data_dir),
                    "compare_fill_models": True,
                    "models": [model for model, _ in COMPARE_FILL_MODELS],
                    "directions": directions,
                },
            )
        )
    if write_csv:
        written["csv"] = str(write_summary_csv(csv_path, results.values()))

    return {
        "symbol": symbol_upper,
        "directions": directions,
        "limit": limit,
        "max_candles": effective_max_candles,
        "candles_loaded": len(candle_rows),
        "compare_fill_models": True,
        "results": results,
        "output_files": written,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run original hedge bot historical backtest on feather candles.",
    )
    parser.add_argument("--symbol", default="APTUSDT", help="Symbol, e.g. APTUSDT")
    parser.add_argument(
        "--direction",
        default="both",
        choices=["long", "short", "both"],
        help="Strategy direction to run",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Load last N candles from feather file",
    )
    parser.add_argument(
        "--max-candles",
        type=int,
        default=None,
        help="Max candles to process after entry (default: same as --limit)",
    )
    parser.add_argument(
        "--data-dir",
        default=str(DEFAULT_DATA_DIR),
        help="Directory containing Bybit futures feather files",
    )
    parser.add_argument(
        "--output-dir",
        default="research/backtests/results",
        help="Directory for JSON/CSV output",
    )
    parser.add_argument(
        "--fill-model",
        default="conservative",
        choices=["conservative", "conservative_multi", "paired_exit"],
        help="5m fill model variant",
    )
    parser.add_argument(
        "--max-fills-per-candle",
        type=int,
        default=None,
        help="Override model default max fills per candle",
    )
    parser.add_argument(
        "--compare-fill-models",
        action="store_true",
        help="Run conservative, conservative_multi, and paired_exit comparison",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Write JSON results (default: write JSON and CSV)",
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        help="Write CSV summary (default: write JSON and CSV)",
    )
    parser.add_argument(
        "--no-json",
        action="store_true",
        help="Skip JSON output",
    )
    parser.add_argument(
        "--no-csv",
        action="store_true",
        help="Skip CSV output",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print extended debug summary per direction",
    )
    parser.add_argument(
        "--print-fill-log",
        action="store_true",
        help="With --debug, print full fill_log",
    )
    parser.add_argument(
        "--print-order-log",
        action="store_true",
        help="With --debug, print full order_log",
    )
    parser.add_argument(
        "--print-intent-log",
        action="store_true",
        help="With --debug, print full intent_log",
    )
    parser.add_argument(
        "--print-exit-diagnostics",
        action="store_true",
        help="With --debug, print full final exit diagnostics",
    )
    return parser


def _print_run_summary(payload: dict[str, Any]) -> None:
    header = (
        f"symbol={payload['symbol']} candles_loaded={payload['candles_loaded']}"
    )
    if payload.get("compare_fill_models"):
        print(f"{header} compare_fill_models=True")
    else:
        print(
            f"{header} fill_model={payload.get('fill_model')} "
            f"max_fills_per_candle={payload.get('max_fills_per_candle')}"
        )

    for key, result in payload["results"].items():
        label = key if payload.get("compare_fill_models") else key
        print(
            f"  {label}: status={result.final_status} "
            f"fills={result.fills_count} pnl={result.realized_pnl:.4f} "
            f"candles={result.candles_processed} exit={result.exit_reason} "
            f"fill_model={result.fill_model} max_fills={result.max_fills_per_candle}"
        )
    if payload["output_files"]["json"]:
        print(f"json={payload['output_files']['json']}")
    if payload["output_files"]["csv"]:
        print(f"csv={payload['output_files']['csv']}")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    write_json = not args.no_json
    write_csv = not args.no_csv
    if args.json:
        write_json = True
    if args.csv:
        write_csv = True
    if args.no_json and args.no_csv and not args.json and not args.csv:
        write_json = True
        write_csv = True

    try:
        if args.compare_fill_models:
            payload = run_fill_model_comparison(
                symbol=args.symbol,
                direction=args.direction,
                limit=args.limit,
                max_candles=args.max_candles,
                data_dir=args.data_dir,
                output_dir=args.output_dir,
                write_json=write_json,
                write_csv=write_csv,
            )
        else:
            payload = run_original_hedge_backtests(
                symbol=args.symbol,
                direction=args.direction,
                limit=args.limit,
                max_candles=args.max_candles,
                data_dir=args.data_dir,
                output_dir=args.output_dir,
                fill_model=args.fill_model,
                max_fills_per_candle=args.max_fills_per_candle,
                write_json=write_json,
                write_csv=write_csv,
            )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    _print_run_summary(payload)

    if args.debug:
        for _, result in payload["results"].items():
            print_debug_report(
                result,
                print_fill_log=args.print_fill_log,
                print_order_log=args.print_order_log,
                print_intent_log=args.print_intent_log,
                print_exit_diagnostics=args.print_exit_diagnostics,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

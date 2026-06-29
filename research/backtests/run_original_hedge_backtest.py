"""CLI runner for original hedge bot historical backtests (Phase 5)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .backtest_report import (
    BacktestResult,
    default_output_paths,
    write_results_json,
    write_summary_csv,
)
from .candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol
from .historical_backtest import run_historical_backtest


def resolve_directions(direction: str) -> list[str]:
    normalized = str(direction or "both").strip().lower()
    if normalized == "both":
        return ["long", "short"]
    if normalized in {"long", "short"}:
        return [normalized]
    raise ValueError(f"unsupported direction: {direction}")


def run_original_hedge_backtests(
    *,
    symbol: str = "APTUSDT",
    direction: str = "both",
    limit: int = 1000,
    max_candles: int | None = None,
    data_dir: str | Path = DEFAULT_DATA_DIR,
    output_dir: str | Path = "research/backtests/results",
    max_fills_per_candle: int = 1,
    write_json: bool = True,
    write_csv: bool = True,
) -> dict[str, Any]:
    symbol_upper = symbol.upper()
    directions = resolve_directions(direction)
    effective_max_candles = max_candles if max_candles is not None else limit

    candles = load_candles_for_symbol(
        symbol_upper,
        timeframe="5m",
        data_dir=data_dir,
        limit=limit,
    )
    if not candles:
        raise FileNotFoundError(
            f"no candles loaded for {symbol_upper} under {data_dir}"
        )

    results: dict[str, BacktestResult] = {}
    for run_direction in directions:
        results[run_direction] = run_historical_backtest(
            symbol_upper,
            run_direction,
            candles,
            max_candles=effective_max_candles,
            max_fills_per_candle=max_fills_per_candle,
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
                    "max_fills_per_candle": max_fills_per_candle,
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
        "candles_loaded": len(candles),
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
        "--max-fills-per-candle",
        type=int,
        default=1,
        help="Conservative max resting fills per 5m candle",
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
    return parser


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
        payload = run_original_hedge_backtests(
            symbol=args.symbol,
            direction=args.direction,
            limit=args.limit,
            max_candles=args.max_candles,
            data_dir=args.data_dir,
            output_dir=args.output_dir,
            max_fills_per_candle=args.max_fills_per_candle,
            write_json=write_json,
            write_csv=write_csv,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"symbol={payload['symbol']} candles_loaded={payload['candles_loaded']}")
    for direction in payload["directions"]:
        result = payload["results"][direction]
        print(
            f"  {direction}: status={result.final_status} "
            f"fills={result.fills_count} pnl={result.realized_pnl:.4f} "
            f"candles={result.candles_processed} exit={result.exit_reason}"
        )
    if payload["output_files"]["json"]:
        print(f"json={payload['output_files']['json']}")
    if payload["output_files"]["csv"]:
        print(f"csv={payload['output_files']['csv']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

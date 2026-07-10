"""CLI: scan for recovery trade candidates and export selected snapshot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol_with_slice_info
from .historical_backtest import normalize_candles
from .recovery_trade_finder import (
    DEFAULT_START_STEP,
    load_hint_start_indices_from_archive,
    run_recovery_trade_finder,
)

DEFAULT_OUTPUT = Path("research/backtests/results/recovery_trade_candidates")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Find recovery trade candidates for gap/swing comparison")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--symbol", default="APTUSDT")
    parser.add_argument("--limit", type=int, default=52569)
    parser.add_argument("--start-step", type=int, default=DEFAULT_START_STEP)
    parser.add_argument("--min-follow-candles", type=int, default=100)
    parser.add_argument("--use-archive-hints", action="store_true")
    parser.add_argument("--archive-root", type=Path, default=None)
    parser.add_argument("--skip-start-index-scan", action="store_true")
    args = parser.parse_args(argv)

    output_dir = args.output_dir.resolve()
    candles_raw, slice_info = load_candles_for_symbol_with_slice_info(
        args.symbol,
        timeframe="5m",
        data_dir=DEFAULT_DATA_DIR,
        limit=args.limit,
    )
    candles = normalize_candles(args.symbol, candles_raw)

    extra_indices: list[int] = []
    if args.use_archive_hints:
        extra_indices = load_hint_start_indices_from_archive(args.archive_root)

    summary = run_recovery_trade_finder(
        output_dir=output_dir,
        candles=candles,
        input_slice_start_index=slice_info.input_slice_start_index,
        start_step=args.start_step,
        min_follow_candles=args.min_follow_candles,
        extra_start_indices=extra_indices,
        skip_start_index_scan=args.skip_start_index_scan,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if summary.get("eligible_count", 0) > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

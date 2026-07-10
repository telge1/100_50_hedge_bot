"""CLI runner for short vs long gross-profit difference analysis."""

from __future__ import annotations

import argparse
from pathlib import Path

from .analyze_short_vs_long_profit_difference import DEFAULT_OUTPUT_DIR, DEFAULT_SOURCE_DIR, run_full_analysis


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze why independent short-primary bot yields less gross profit than long-primary."
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=DEFAULT_SOURCE_DIR,
        help="Directory with independent continuous long/short results",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for analysis artifacts",
    )
    parser.add_argument(
        "--skip-neutral-control",
        action="store_true",
        help="Skip optional neutral-config diagnostic backtests",
    )
    parser.add_argument(
        "--neutral-control-limit",
        type=int,
        default=52569,
        help="Candle limit for neutral control backtests",
    )
    args = parser.parse_args()
    summary = run_full_analysis(
        source_dir=args.source_dir,
        output_dir=args.output_dir,
        skip_neutral_control=args.skip_neutral_control,
        neutral_control_limit=args.neutral_control_limit,
    )
    print(f"Analysis written to {summary['output_dir']}")
    print(f"Long gross profit: {summary['long_summary']['gross_profit']:.2f}")
    print(f"Short gross profit: {summary['short_summary']['gross_profit']:.2f}")


if __name__ == "__main__":
    main()

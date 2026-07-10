"""CLI runner for short cycle-1 second leg audit."""

from __future__ import annotations

import argparse
from pathlib import Path

from .audit_short_cycle1_second_leg import DEFAULT_OUTPUT_DIR, DEFAULT_SOURCE_DIR, run_audit


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit short cycle-1 second leg (analysis only)")
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=DEFAULT_SOURCE_DIR,
        help="Independent backtest results directory",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Audit output directory",
    )
    args = parser.parse_args()
    summary = run_audit(source_dir=args.source_dir, output_dir=args.output_dir)
    print(f"Audit written to {args.output_dir.resolve()}")
    print(f"Population: {summary['population_trades']} trades")
    print(f"Second leg created: {summary['second_leg_created_count']}")
    print(f"Primary cause: {summary['primary_classification']}")


if __name__ == "__main__":
    main()

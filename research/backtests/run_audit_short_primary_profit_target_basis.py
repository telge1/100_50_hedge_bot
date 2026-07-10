"""CLI runner for short-primary profit target basis audit."""

from __future__ import annotations

import argparse
from pathlib import Path

from .audit_short_primary_profit_target_basis import DEFAULT_OUTPUT_DIR, DEFAULT_SOURCE_DIR, run_audit


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit short-primary profit target notional basis.")
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    summary = run_audit(source_dir=args.source_dir, output_dir=args.output_dir)
    print(f"Audit written to {summary['output_dir']}")
    print(f"Hypothesis: {summary['hypothesis_evaluation']['conclusion']}")


if __name__ == "__main__":
    main()

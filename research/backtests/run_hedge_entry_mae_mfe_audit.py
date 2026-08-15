#!/usr/bin/env python3
"""CLI: read-only hedge entry MAE/MFE audit."""

from __future__ import annotations

import argparse
from pathlib import Path

from research.backtests.hedge_entry_mae_mfe_audit.core import DEFAULT_OUT, DEFAULT_SOURCES, run_audit


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--source",
        action="append",
        type=Path,
        default=None,
        help="Continuous results JSON (repeatable). Default: long + short APT continuous corpora.",
    )
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    args = p.parse_args()
    sources = args.source if args.source else list(DEFAULT_SOURCES)
    summary = run_audit(source_jsons=sources, output_dir=args.output_dir)
    print(f"primary_decision={summary['primary_decision']}")
    print(f"n_trades={summary['n_trades']} n_successful={summary['n_successful']}")
    print(f"wrote {args.output_dir}")


if __name__ == "__main__":
    main()

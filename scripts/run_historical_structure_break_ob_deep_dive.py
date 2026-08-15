#!/usr/bin/env python3
"""CLI: historical structure-break OB deep dive (research only)."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.orderbook.historical_structure_break_ob_deep_dive.run import (
    DEFAULT_OUT,
    run_deep_dive,
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--max-events", type=int, default=15)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    summary = run_deep_dive(out_dir=args.out_dir, max_events=args.max_events)
    print("PRIMARY_DECISION", summary["primary_decision"])
    print("N_CLUSTERED", summary["n_clustered_events"])
    print("N_SELECTED", summary["n_selected"])
    print("CLASSIFICATIONS", summary["classification_counts"])
    print("QUALITY", summary["quality_counts"])
    print("OUT", args.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

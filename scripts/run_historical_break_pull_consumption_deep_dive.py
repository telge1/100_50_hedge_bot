#!/usr/bin/env python3
"""CLI: historical break pull vs consumption deep dive (15 events)."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.orderbook.historical_break_pull_consumption.run import (  # noqa: E402
    DEFAULT_EVENTS,
    DEFAULT_OUT,
    run_deep_dive,
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--events-csv", type=Path, default=DEFAULT_EVENTS)
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    summary = run_deep_dive(events_csv=args.events_csv, out_dir=args.out_dir)
    print("PRIMARY_DECISION", summary["primary_decision"])
    print("MECHANISMS", summary["mechanism_counts"])
    print("QUALITY", summary["quality_counts"])
    print("OUT", args.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

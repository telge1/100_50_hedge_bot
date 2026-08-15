#!/usr/bin/env python3
"""CLI: CH break/reclaim microstructure audit (research only)."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.orderbook.ch_break_reclaim_microstructure_audit.run import DEFAULT_OUT, run_audit


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--limit", type=int, default=None, help="optional event cap for smoke")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    summary = run_audit(out_dir=args.out_dir, limit=args.limit)
    print("PRIMARY_DECISION", summary["primary_decision"])
    print("OUT", args.out_dir)
    print("N_EVENTS", summary["n_events"])
    print("QUALITY", summary["data_quality_counts"])
    print("OUTCOMES", summary["outcome_counts"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

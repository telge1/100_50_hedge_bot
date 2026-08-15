#!/usr/bin/env python3
"""CLI: subgroup validation of early break/reclaim OB signals."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.orderbook.ch_break_reclaim_subgroup_validation.run import (
    DEFAULT_OUT,
    run_subgroup_validation,
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    args = p.parse_args()
    summary = run_subgroup_validation(out_dir=args.out_dir)
    print("PRIMARY_DECISION", summary["primary_decision"])
    print("N_EVENTS", summary["n_events_data_valid_non_excluded"])
    print("STRONGEST", summary.get("strongest_subgroup"))
    print("OUT", args.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

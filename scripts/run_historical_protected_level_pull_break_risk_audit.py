#!/usr/bin/env python3
"""CLI: historical protected-level pull vs break-risk audit."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.orderbook.historical_protected_level_pull_break_risk import (  # noqa: E402
    DEFAULT_OB_ROOT,
    DEFAULT_OUT,
    DEFAULT_TRADE_ROOT,
)
from research.orderbook.historical_protected_level_pull_break_risk.run import run_audit  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--ob-root", type=Path, default=DEFAULT_OB_ROOT)
    p.add_argument("--trade-root", type=Path, default=DEFAULT_TRADE_ROOT)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    summary = run_audit(out_dir=args.out, ob_root=args.ob_root, trade_root=args.trade_root)
    c = summary["counts"]
    print("PRIMARY_DECISION", summary["primary_decision"])
    print(
        "APPROACHES",
        c["total"],
        "BREAK",
        c["LEVEL_BREAK"],
        "HOLD",
        c["LEVEL_HOLD_REJECT"],
        "AMBIGUOUS",
        c["AMBIGUOUS"],
    )
    print("BEST_PULL_FEATURE", summary["best_pull_feature"])
    a = summary["aucs"]
    print("AUC_PULL", a.get("auc_pull_only"))
    print("AUC_DISTANCE", a.get("auc_distance_only"))
    print("AUC_DISTANCE_PLUS_PULL", a.get("auc_distance_plus_pull"))
    print("EARLIEST", summary["earliest_separation"])
    print("MEDIAN_LEAD_S", summary["median_seconds_anchor_to_break"])
    print("STRONGEST_SUBGROUP", summary["strongest_subgroup"])
    print("ARTIFACTS", summary["artifact_dir"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

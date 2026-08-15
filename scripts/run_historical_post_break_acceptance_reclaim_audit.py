#!/usr/bin/env python3
"""CLI: historical post-break acceptance vs reclaim audit."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.orderbook.historical_post_break_acceptance_reclaim import (  # noqa: E402
    DEFAULT_OB_ROOT,
    DEFAULT_OUT,
    DEFAULT_SELECTED,
    DEFAULT_TRADE_ROOT,
)
from research.orderbook.historical_post_break_acceptance_reclaim.run import run_audit  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--selected", type=Path, default=DEFAULT_SELECTED)
    p.add_argument("--ob-root", type=Path, default=DEFAULT_OB_ROOT)
    p.add_argument("--trade-root", type=Path, default=DEFAULT_TRADE_ROOT)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    s = run_audit(
        out_dir=args.out,
        selected_path=args.selected,
        ob_root=args.ob_root,
        trade_root=args.trade_root,
    )
    c = s["counts"]
    print("PRIMARY_DECISION", s["primary_decision"])
    print("N_ACCEPTED", c.get("BREAK_ACCEPTED"), "N_RECLAIM", c.get("RECLAIM"))
    for row in s.get("cutoff_results") or []:
        if int(row["cutoff"]) in {5, 10, 20, 30}:
            print(
                f"+{row['cutoff']}s",
                "price",
                row.get("best_price_auc"),
                "ob",
                row.get("best_ob_auc"),
                "flow",
                row.get("best_flow_auc"),
                "dist",
                row.get("auc_distance_only"),
                "combo",
                row.get("auc_distance_plus_ob_flow"),
            )
    print("BEST_PRICE", s["best_price_feature_at_10s"].get("feature"), s["best_price_feature_at_10s"].get("auc"))
    print("BEST_OB", s["best_ob_feature_at_10s"].get("feature"), s["best_ob_feature_at_10s"].get("auc"))
    print("BEST_FLOW", s["best_flow_feature_at_10s"].get("feature"), s["best_flow_feature_at_10s"].get("auc"))
    d = s.get("distance_control_at_focus") or {}
    print("DIST_ONLY", d.get("auc_distance_only"), "DIST_OB_FLOW", d.get("auc_distance_plus_ob_flow"))
    print("EARLIEST", s["earliest_useful_time"])
    print("JACKKNIFE", s.get("jackknife"))
    print("ARTIFACTS", s["artifact_dir"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

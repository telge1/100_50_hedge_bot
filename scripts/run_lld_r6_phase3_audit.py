#!/usr/bin/env python3
"""CLI: Phase-3 R6 leakage + raw OB audit (read-only)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.liquidity_location_r6_phase3_audit.runner import run_audit  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--phase3-dir",
        type=Path,
        default=ROOT / "results" / "liquidity_location_r6_orderflow_confirmation_v1",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "results" / "liquidity_location_r6_phase3_audit",
    )
    args = p.parse_args()
    res = run_audit(phase3_dir=args.phase3_dir, out_dir=args.out_dir)
    print(res.get("verdict"))
    print(res.get("manifest", {}).get("phase4_recommendation"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

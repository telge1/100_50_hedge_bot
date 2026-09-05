#!/usr/bin/env python3
"""CLI: R6 orderflow confirmation Phase-3 (read-only)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.liquidity_location_r6_orderflow_confirmation_v1.runner import (  # noqa: E402
    DEFAULT_OUT,
    run_phase3,
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--primary-t3-sec", type=int, default=30)
    p.add_argument("--n-boot", type=int, default=200)
    args = p.parse_args()
    res = run_phase3(
        out_dir=args.out_dir,
        primary_t3_sec=args.primary_t3_sec,
        n_boot=args.n_boot,
    )
    print(res.get("verdict"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

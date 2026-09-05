#!/usr/bin/env python3
"""CLI: causal Liquidity Location pool lifecycle smoke (read-only)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.liquidity_location_pool_lifecycle.runner import (  # noqa: E402
    DEFAULT_OUT,
    coverage_probe,
    run_smoke,
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--window-days", type=int, default=30)
    p.add_argument("--warmup-days", type=int, default=14)
    p.add_argument("--coverage-only", action="store_true")
    p.add_argument(
        "--max-pools-per-tf",
        type=int,
        default=None,
        help="Optional cap for faster debug smokes",
    )
    args = p.parse_args()
    if args.coverage_only:
        cov = coverage_probe(window_days=args.window_days, warmup_days=args.warmup_days)
        print(cov)
        return 0
    res = run_smoke(
        out_dir=args.out_dir,
        window_days=args.window_days,
        warmup_days=args.warmup_days,
        max_pools_per_tf=args.max_pools_per_tf,
        coverage_only=False,
    )
    print(res.get("verdict"))
    return 0 if "SMOKE_COMPLETE" in str(res.get("verdict")) else 1


if __name__ == "__main__":
    raise SystemExit(main())

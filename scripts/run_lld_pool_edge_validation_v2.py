#!/usr/bin/env python3
"""CLI: Liquidity Location pool edge validation V2 (read-only)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.liquidity_location_pool_edge_validation_v2.runner import (  # noqa: E402
    DEFAULT_OUT,
    run_v2,
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument(
        "--v1-dir",
        type=Path,
        default=ROOT / "results" / "liquidity_location_pool_lifecycle_v1",
    )
    p.add_argument("--skip-approach", action="store_true")
    p.add_argument("--n-boot", type=int, default=300)
    args = p.parse_args()
    res = run_v2(
        v1_dir=args.v1_dir,
        out_dir=args.out_dir,
        skip_approach=args.skip_approach,
        n_boot=args.n_boot,
    )
    print(res.get("verdict"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

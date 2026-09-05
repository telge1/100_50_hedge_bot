#!/usr/bin/env python3
"""CLI: causal feature repair smoke + raw OB archive diagnosis."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.liquidity_location_r6_causal_and_raw_audit_v2.runner import (  # noqa: E402
    run_audit_v2,
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--n-smoke", type=int, default=8)
    p.add_argument("--skip-full-raw-audit", action="store_true")
    p.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "results" / "liquidity_location_r6_causal_and_raw_audit_v2",
    )
    args = p.parse_args()
    res = run_audit_v2(
        out_dir=args.out_dir,
        n_smoke=args.n_smoke,
        audit_all_raw=not args.skip_full_raw_audit,
    )
    print(res["verdict"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""CLI: A+ Liquidity Pool Signal Scanner V1 (research-only)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1 import VERDICT_CODE_READY  # noqa: E402
from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.runner import run_doge_smoke  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out-dir", type=Path, default=None)
    p.add_argument("--days", type=int, default=3)
    args = p.parse_args()
    res = run_doge_smoke(out_dir=args.out_dir, days=args.days)
    print(VERDICT_CODE_READY)
    print(res["manifest"].get("verdict"))
    print("confirmed", res["result"].get("n_confirmed"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

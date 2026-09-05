#!/usr/bin/env python3
"""Run one DOGE research live-shadow pass (no daemon, no orders)."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.live_shadow import run_doge_shadow_once


def main() -> int:
    p = argparse.ArgumentParser(description="DOGE A+ V2 research shadow (single pass)")
    p.add_argument("--end", type=str, default=None, help="ISO end timestamp UTC")
    p.add_argument("--out-dir", type=Path, default=None)
    args = p.parse_args()
    end = datetime.fromisoformat(args.end.replace("Z", "")) if args.end else None
    out = run_doge_shadow_once(end=end, out_dir=args.out_dir)
    print(out["manifest"].get("run_id"), out["out_dir"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

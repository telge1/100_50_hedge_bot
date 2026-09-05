#!/usr/bin/env python3
"""CLI: DOGEUSDT reference replay V2 validation (research-only)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.doge_reference_replay_v2 import (  # noqa: E402
    run_doge_reference_replay_v2,
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out-dir", type=Path, default=None)
    args = p.parse_args()
    out = run_doge_reference_replay_v2(out_dir=args.out_dir)
    print(out["manifest"]["verdict"])
    print("out_dir", out["out_dir"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Autonomous continuous discovery for ema_zone_microstructure_confirmation_v1.

No manual windows. No trade compiler / entry / exit / PnL.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.ema_zone_microstructure_confirmation.continuous_runner import run


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--smoke-hours", type=float, default=None, help="Limit discovery duration")
    p.add_argument("--smoke-symbol", type=str, default=None)
    p.add_argument("--symbols", type=str, default="BTCUSDT,DOGEUSDT")
    p.add_argument("--out-subdir", type=str, default=None)
    args = p.parse_args()
    symbols = tuple(s.strip() for s in args.symbols.split(",") if s.strip())
    raw = ROOT / "data/orderbook_raw_shadow/ob200_v3"
    out = ROOT / "results/ema_zone_microstructure_confirmation"
    run(
        repo_root=ROOT,
        raw_root=raw,
        out_root=out,
        symbols=symbols,
        smoke_hours=args.smoke_hours,
        smoke_symbol=args.smoke_symbol,
        out_subdir=args.out_subdir,
    )


if __name__ == "__main__":
    main()

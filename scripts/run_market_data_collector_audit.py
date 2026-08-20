#!/usr/bin/env python3
"""Phase-1 market data collector / coverage audit (read-only)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
src = ROOT / "src"
if str(src) not in sys.path:
    sys.path.insert(0, str(src))

from orderbook_analyse.market_data_coverage.collector_audit import run_full_audit


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Collector coverage audit (read-only).")
    p.add_argument("--symbols", default="DOGEUSDT,APTUSDT,BTCUSDT")
    p.add_argument("--lookback-hours", type=float, default=24.0)
    p.add_argument("--output-dir", type=Path, default=ROOT / "results/collector_coverage_audit")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    summary = run_full_audit(
        root=ROOT,
        symbols=symbols,
        output_dir=args.output_dir,
        lookback_hours=float(args.lookback_hours),
    )
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

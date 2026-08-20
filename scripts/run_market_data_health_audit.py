#!/usr/bin/env python3
"""Market data health audit."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
src = ROOT / "src"
if str(src) not in sys.path:
    sys.path.insert(0, str(src))

from orderbook_analyse.market_data_coverage.health import run_health_audit


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", default="DOGEUSDT,APTUSDT,BTCUSDT")
    p.add_argument("--lookback-minutes", type=int, default=60)
    p.add_argument("--stale-seconds", type=float, default=180)
    p.add_argument("--wall-stale-seconds", type=float, default=3600)
    p.add_argument("--minimum-trades-per-minute", type=float, default=0)
    p.add_argument("--minimum-orderbook-updates-per-minute", type=float, default=0)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--fail-on-unhealthy", action="store_true")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    summary, code = run_health_audit(
        symbols=symbols,
        lookback_minutes=args.lookback_minutes,
        stale_seconds=args.stale_seconds,
        wall_stale_seconds=args.wall_stale_seconds,
        output_dir=args.output_dir,
        fail_on_unhealthy=args.fail_on_unhealthy,
    )
    print(f"DECISION={summary['decision']} EXIT={code}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

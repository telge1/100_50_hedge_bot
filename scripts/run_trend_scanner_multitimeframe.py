#!/usr/bin/env python3
"""CLI: Multi-timeframe Protected High/Low structure scan."""

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

from orderbook_analyse.trend_scanner_multitimeframe import (  # noqa: E402
    DEFAULT_CANDLE_DIR,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_SCANNER_ROOT,
    run_trend_scanner_multitimeframe,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Multi-timeframe C3.4B Protected High/Low structure scan (research only)"
    )
    p.add_argument(
        "--symbols",
        default="APTUSDT,DOGEUSDT,BTCUSDT",
        help="Comma-separated symbols",
    )
    p.add_argument(
        "--timeframes",
        default="5m,1h,4h",
        help="Comma-separated timeframes (default: 5m,1h,4h)",
    )
    p.add_argument("--candle-dir", type=Path, default=DEFAULT_CANDLE_DIR)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--scanner-root", type=Path, default=DEFAULT_SCANNER_ROOT)
    p.add_argument("--warmup-bars", type=int, default=72)
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    symbols = [s.strip().upper() for s in str(args.symbols).split(",") if s.strip()]
    timeframes = [t.strip().lower() for t in str(args.timeframes).split(",") if t.strip()]
    result = run_trend_scanner_multitimeframe(
        symbols=symbols,
        timeframes=timeframes,
        candle_dir=args.candle_dir,
        output_dir=args.output_dir,
        overwrite=bool(args.overwrite),
        scanner_root=args.scanner_root,
        warmup_bars=int(args.warmup_bars),
    )
    print(
        json.dumps(
            {
                "primary_decision": result["decision"],
                "decision_note": result["decision_note"],
                "output_dir": result["output_dir"],
                "parity_pass": result["parity"].get("pass"),
                "n_pl_events": result["n_pl_events"],
                "n_ph_events": result["n_ph_events"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

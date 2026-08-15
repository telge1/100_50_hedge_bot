#!/usr/bin/env python3
"""Download Bybit futures HTF OHLCV via Freqtrade into a staging datadir.

Does not touch the canonical research feather tree under
``Signal_Generator_Ralf/data/bybit/futures``. Never uses ``--erase``.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

FREQTRADE_BIN = Path("/home/telgenbuescher/projects/freqtrade/.venv/bin/freqtrade")
FREQTRADE_CONFIG = Path("/home/telgenbuescher/projects/freqtrade/user_data/config.json")
DEFAULT_STAGING = Path(
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/data_htf_candle_staging"
)

DEFAULT_PAIRS = (
    "APT/USDT:USDT",
    "DOGE/USDT:USDT",
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
)
# Bybit/CCXT/Freqtrade labels (1M is monthly, case-sensitive).
DEFAULT_TIMEFRAMES = ("4h", "1d", "1w", "1M")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--datadir", type=Path, default=DEFAULT_STAGING)
    p.add_argument("--pairs", nargs="+", default=list(DEFAULT_PAIRS))
    p.add_argument("--timeframes", nargs="+", default=list(DEFAULT_TIMEFRAMES))
    p.add_argument(
        "--timerange",
        default="20180101-",
        help="Freqtrade timerange (default: 20180101- = as far back as available)",
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    if not FREQTRADE_BIN.is_file():
        print(f"ERROR: freqtrade binary missing: {FREQTRADE_BIN}", file=sys.stderr)
        return 2
    args.datadir.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(FREQTRADE_BIN),
        "download-data",
        "--config",
        str(FREQTRADE_CONFIG),
        "--datadir",
        str(args.datadir),
        "--pairs",
        *args.pairs,
        "--timeframes",
        *args.timeframes,
        "--timerange",
        args.timerange,
        "--trading-mode",
        "futures",
        "--data-format-ohlcv",
        "feather",
    ]
    print("CMD:", " ".join(cmd), flush=True)
    if args.dry_run:
        return 0
    # cwd = freqtrade project so relative paths in config resolve
    return int(
        subprocess.call(
            cmd,
            cwd="/home/telgenbuescher/projects/freqtrade",
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())

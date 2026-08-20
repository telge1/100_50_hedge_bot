#!/usr/bin/env python3
"""Download Bybit public-trade day files from public.bybit.com (no cookies).

Example:
  PYTHONPATH=src python scripts/download_bybit_public_trades.py \\
    --symbol APTUSDT \\
    --start 2026-07-24T00:00:00Z \\
    --end 2026-07-30T23:59:59Z \\
    --dest imports/apt_public_trades_july/gz
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.public_trade_source.downloader import (  # noqa: E402
    RETRIABLE_STATUSES,
    STATUS_SOURCE_MISSING,
    PublicTradeDayDownloader,
)


def _parse_utc(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    ts = datetime.fromisoformat(text)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", required=True)
    p.add_argument("--start", required=True, help="UTC, overlapping calendar days are downloaded")
    p.add_argument("--end", required=True)
    p.add_argument("--dest", type=Path, required=True)
    p.add_argument("--no-head", action="store_true")
    p.add_argument("--checkpoint", type=Path, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dl = PublicTradeDayDownloader(
        args.dest,
        checkpoint_path=args.checkpoint,
        use_head=not args.no_head,
    )
    results = dl.download_range(args.symbol, _parse_utc(args.start), _parse_utc(args.end))
    print(json.dumps({"symbol": args.symbol.upper(), "results": [r.to_dict() for r in results]}, indent=2))
    if any(r.status in RETRIABLE_STATUSES or r.status == "FAILED" for r in results):
        return 3
    if any(r.status == STATUS_SOURCE_MISSING for r in results):
        return 2
    if any(r.status not in ("COMPLETE", "SKIPPED_UNCHANGED") for r in results):
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

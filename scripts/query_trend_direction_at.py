#!/usr/bin/env python3
"""CLI: historical MySQL 5m structure direction at a UTC timestamp (read-only)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.regime_scanner.trend_direction_at import (  # noqa: E402
    TrendDirectionAtError,
    format_text_report,
    query_trend_direction_at,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Query C3.4B 5m trend direction (BULLISH/BEARISH/UNCLEAR) at a historical "
            "UTC timestamp using only MySQL candles with close_time <= T."
        )
    )
    p.add_argument("--symbol", required=True, help="e.g. APTUSDT / aptusdt")
    p.add_argument("--timestamp", required=True, help="ISO-8601 UTC, e.g. 2026-04-11T20:31:00Z")
    p.add_argument("--exchange", default="bybit")
    p.add_argument("--timeframe", default="5m", help="Primary TF (must be 5m)")
    p.add_argument("--output", choices=("text", "json"), default="text")
    p.add_argument(
        "--env-file",
        default="research/regime_scanner/.env.regime_db",
        help="Path to REGIME_DB_* env file",
    )
    p.add_argument("--warmup-bars", type=int, default=72)
    p.add_argument(
        "--htf",
        action="store_true",
        help="Include optional 15m/30m HTF diagnostic (slower; not used for primary direction)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = query_trend_direction_at(
            symbol=args.symbol,
            timestamp=args.timestamp,
            exchange=args.exchange,
            timeframe=args.timeframe,
            warmup_bars=int(args.warmup_bars),
            env_file=args.env_file,
            include_htf=bool(args.htf),
        )
    except TrendDirectionAtError as exc:
        payload = {
            "error": True,
            "reason": exc.reason,
            "message": exc.message,
            "symbol": args.symbol,
            "timestamp": args.timestamp,
        }
        if args.output == "json":
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"ERROR [{exc.reason}]: {exc.message}", file=sys.stderr)
        # coverage / data errors → 2; config/symbol → 1
        if exc.reason in {
            "TIMESTAMP_BEFORE_DATA",
            "TIMESTAMP_AFTER_DATA",
            "NO_CLOSED_CANDLE",
            "LOOKAHEAD_VIOLATION",
        }:
            return 2
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR [UNEXPECTED]: {exc}", file=sys.stderr)
        return 1

    if args.output == "json":
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True, default=str))
    else:
        print(format_text_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

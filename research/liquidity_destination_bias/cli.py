"""CLI for the bounded, historical Phase-1 episode builder."""

from __future__ import annotations

import argparse
import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path

from .builder import (
    DEFAULT_TRADE_CHUNK_SECONDS,
    BuildConfig,
    build_episodes,
    write_artifacts,
)
from .contract import TARGET_SOURCE


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def parse_chunk_hours(value: str) -> int:
    hours = float(value)
    if not math.isfinite(hours) or hours <= 0 or hours > 2:
        raise argparse.ArgumentTypeError("chunk-hours must be in (0,2]")
    return int(round(hours * 60 * 60))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Build frozen causal liquidity-destination episodes (research only)."
    )
    p.add_argument("--symbol", required=True, choices=("BTCUSDT", "DOGEUSDT"))
    p.add_argument("--start", required=True, type=parse_utc)
    p.add_argument("--end", required=True, type=parse_utc)
    p.add_argument("--horizon-minutes", required=True, type=int)
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument("--require-complete", action="store_true")
    p.add_argument("--max-episodes", type=int)
    p.add_argument("--target-source", default=TARGET_SOURCE, choices=(TARGET_SOURCE,))
    p.add_argument(
        "--allow-bounded-expand",
        action="store_true",
        help=(
            "Explicitly permit a run longer than two hours. The run remains "
            "single-worker and uses bounded read-only trade chunks."
        ),
    )
    p.add_argument(
        "--max-window-hours",
        type=float,
        help=(
            "Required safety ceiling with --allow-bounded-expand; must be in "
            "(0,168] and at least the requested candidate window."
        ),
    )
    p.add_argument(
        "--chunk-hours",
        dest="trade_chunk_seconds",
        type=parse_chunk_hours,
        default=DEFAULT_TRADE_CHUNK_SECONDS,
        help=(
            "Internal sequential trade-query chunk size in hours, in (0,2]. "
            "Smaller values reduce peak RAM but add bounded query overlap."
        ),
    )
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    config = BuildConfig(
        symbol=args.symbol,
        start=args.start,
        end=args.end,
        horizon_minutes=args.horizon_minutes,
        require_complete=args.require_complete,
        target_source=args.target_source,
        max_episodes=args.max_episodes,
        allow_bounded_expand=args.allow_bounded_expand,
        max_window_hours=args.max_window_hours,
        trade_chunk_seconds=args.trade_chunk_seconds,
    )
    result = build_episodes(config)
    hashes = write_artifacts(result, args.output_dir)
    print(json.dumps({**result.summary, "core_hashes": hashes}, sort_keys=True))
    if args.require_complete and not result.episodes:
        return 4
    return 0

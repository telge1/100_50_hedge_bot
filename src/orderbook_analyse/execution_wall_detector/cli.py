"""CLI for offline EXECUTION_WALL detector."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from orderbook_analyse.execution_wall_detector.analysis import run_execution_wall_detector
from orderbook_analyse.execution_wall_detector.types import (
    DEFAULT_DISTANCE_BANDS_BPS,
    DEFAULT_FORWARD_SECONDS,
    ExecutionWallParams,
)
from orderbook_analyse.wall_toxicity_audit.data_access import parse_utc


def _parse_floats(text: str) -> tuple[float, ...]:
    parts = [p.strip() for p in str(text).replace(";", ",").split(",") if p.strip()]
    return tuple(float(p) for p in parts)


def _parse_ints(text: str) -> tuple[int, ...]:
    parts = [p.strip() for p in str(text).replace(";", ",").split(",") if p.strip()]
    return tuple(int(p) for p in parts)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Offline EXECUTION_WALL detector (near-market; read-only)."
    )
    p.add_argument("--symbol", required=True)
    p.add_argument("--start", required=True, help="UTC start, e.g. '2026-07-26 09:00:00'")
    p.add_argument("--end", required=True, help="UTC end")
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--log-level", default="INFO")

    p.add_argument("--max-distance-bps", type=float, default=30.0)
    p.add_argument(
        "--distance-bands",
        default=",".join(str(int(x)) if x == int(x) else str(x) for x in DEFAULT_DISTANCE_BANDS_BPS),
        help="Comma-separated bps edges, e.g. 0,5,10,20,30,50",
    )
    p.add_argument(
        "--bucket-mode",
        choices=("exact", "ticks", "bps"),
        default="ticks",
        help="Default ticks (1 tick) — avoid auto_10bps mixing of micro levels",
    )
    p.add_argument("--bucket-ticks", type=int, default=1)
    p.add_argument("--bucket-bps", type=float, default=2.0)
    p.add_argument("--sample-interval-ms", type=float, default=500.0)
    p.add_argument("--local-radius-ticks", type=int, default=8)
    p.add_argument("--local-multiple-min", type=float, default=3.0)
    p.add_argument("--local-percentile-min", type=float, default=95.0)
    p.add_argument("--local-depth-share-min", type=float, default=0.10)
    p.add_argument("--near-touch-bps", type=float, default=10.0)
    p.add_argument("--near-touch-percentile-min", type=float, default=80.0)
    p.add_argument("--near-touch-multiple-min", type=float, default=2.0)
    p.add_argument("--near-touch-rank-max", type=int, default=3)
    p.add_argument("--min-level-qty", type=float, default=50.0)
    p.add_argument("--min-level-notional", type=float, default=25.0)
    p.add_argument("--min-lifetime-ms", type=float, default=250.0)
    p.add_argument("--touch-bps", type=float, default=5.0)
    p.add_argument("--break-bps", type=float, default=5.0)
    p.add_argument("--touch-ticks", type=float, default=2.0)
    p.add_argument("--break-ticks", type=float, default=2.0)
    p.add_argument("--acceptance-seconds", type=float, default=15.0)
    p.add_argument("--failed-break-return-seconds", type=float, default=60.0)
    p.add_argument("--trade-match-window-ms", type=float, default=400.0)
    p.add_argument("--chunk-minutes", type=float, default=15.0)
    p.add_argument("--tick-size", type=float, default=None)
    p.add_argument(
        "--forward-seconds",
        default=",".join(str(x) for x in DEFAULT_FORWARD_SECONDS),
    )
    p.add_argument(
        "--structure-sequences-csv",
        default=None,
        help="Optional STRUCTURE wall_sequences.csv for comparison",
    )
    return p


def params_from_args(args: argparse.Namespace) -> ExecutionWallParams:
    return ExecutionWallParams(
        max_distance_bps=args.max_distance_bps,
        distance_bands_bps=_parse_floats(args.distance_bands),
        bucket_mode=args.bucket_mode,
        bucket_ticks=args.bucket_ticks,
        bucket_bps=args.bucket_bps,
        sample_interval_ms=args.sample_interval_ms,
        local_radius_ticks=args.local_radius_ticks,
        local_multiple_min=args.local_multiple_min,
        local_percentile_min=args.local_percentile_min,
        local_depth_share_min=args.local_depth_share_min,
        near_touch_bps=args.near_touch_bps,
        near_touch_percentile_min=args.near_touch_percentile_min,
        near_touch_multiple_min=args.near_touch_multiple_min,
        near_touch_rank_max=args.near_touch_rank_max,
        min_level_qty=args.min_level_qty,
        min_level_notional=args.min_level_notional,
        min_lifetime_ms=args.min_lifetime_ms,
        touch_bps=args.touch_bps,
        break_bps=args.break_bps,
        touch_ticks=args.touch_ticks,
        break_ticks=args.break_ticks,
        acceptance_seconds=args.acceptance_seconds,
        failed_break_return_seconds=args.failed_break_return_seconds,
        trade_match_window_ms=args.trade_match_window_ms,
        chunk_minutes=args.chunk_minutes,
        forward_seconds=_parse_ints(args.forward_seconds),
        tick_size=args.tick_size,
        structure_sequences_csv=args.structure_sequences_csv,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    start = parse_utc(args.start)
    end = parse_utc(args.end)
    if start is None or end is None or end <= start:
        parser.error("invalid --start/--end")
    params = params_from_args(args)
    result = run_execution_wall_detector(
        symbol=args.symbol.upper(),
        start=start,
        end=end,
        output_dir=args.output_dir,
        params=params,
        overwrite=bool(args.overwrite),
    )
    s = result.report.get("summary", {})
    print(
        f"EXECUTION_WALL done: sequences={s.get('sequences')} "
        f"touches={s.get('touches')} touch_rate={s.get('touch_rate')} "
        f"out={args.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""CLI for Breakout X-Ray V1."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .execution_hold import DEFAULT_SILVER_LOCK, assert_lock_untouched, read_builder_lock
from .models import EdgeSide, ManualWindowConfig, StrategyEdgeConfig
from .run import run_manual_window_analysis, run_strategy_edge_analysis
from .time_windows import parse_utc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="obfull_research_engine.breakout_xray_v1",
        description="Breakout X-Ray V1 (strategy-edge | manual-window). "
        "Full live runs blocked while Silver builder lock is active.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--symbol", required=True)
        sp.add_argument("--local-band-usd", type=float, default=400.0)
        sp.add_argument("--output-dir", required=True)
        sp.add_argument("--check-only", action="store_true")
        sp.add_argument(
            "--lock-path",
            type=Path,
            default=DEFAULT_SILVER_LOCK,
            help="Silver builder lock path (read-only probe)",
        )

    s = sub.add_parser("strategy-edge", help="Causal 30m TPO edge strategy analysis")
    add_common(s)
    s.add_argument("--decision-time-utc", required=True)
    s.add_argument("--edge-side", choices=["upper", "lower"], required=True)
    s.add_argument("--pre-window-minutes", type=int, default=30)
    s.add_argument("--post-window-minutes", type=int, default=60)
    s.add_argument("--reference-price", type=float, default=None)
    s.add_argument("--reference-known-as-of-utc", default=None)

    m = sub.add_parser("manual-window", help="Neutral/forensic free window analysis")
    add_common(m)
    m.add_argument("--start-utc", required=True)
    m.add_argument("--end-utc", required=True)
    m.add_argument("--reference-price", type=float, default=None)
    m.add_argument("--reference-side", choices=["upper", "lower"], default=None)
    m.add_argument("--reference-known-as-of-utc", default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    lock_path: Path = args.lock_path

    # Always allow --help via argparse. Probe lock for transparency.
    probe = read_builder_lock(lock_path)
    print(
        f"lock exists={probe.exists} pid={probe.pid} alive={probe.pid_alive} "
        f"blocks_full_run={probe.blocks_full_run}",
        file=sys.stderr,
    )

    if args.command == "strategy-edge":
        cfg = StrategyEdgeConfig(
            symbol=args.symbol,
            decision_time_utc=parse_utc(args.decision_time_utc),
            edge_side=EdgeSide(args.edge_side),
            pre_window_minutes=args.pre_window_minutes,
            post_window_minutes=args.post_window_minutes,
            local_band_usd=args.local_band_usd,
            output_dir=args.output_dir,
            check_only=bool(args.check_only),
            reference_price=args.reference_price,
            reference_known_as_of_utc=(
                None
                if not args.reference_known_as_of_utc
                else parse_utc(args.reference_known_as_of_utc)
            ),
        )
        if not cfg.check_only and probe.blocks_full_run:
            print(probe.reason, file=sys.stderr)
            return 2
        result = run_strategy_edge_analysis(cfg, lock_path=lock_path)
    else:
        side = EdgeSide(args.reference_side) if args.reference_side else None
        cfg_m = ManualWindowConfig(
            symbol=args.symbol,
            start_utc=parse_utc(args.start_utc),
            end_utc=parse_utc(args.end_utc),
            local_band_usd=args.local_band_usd,
            output_dir=args.output_dir,
            check_only=bool(args.check_only),
            reference_price=args.reference_price,
            reference_side=side,
            reference_known_as_of_utc=(
                None
                if not args.reference_known_as_of_utc
                else parse_utc(args.reference_known_as_of_utc)
            ),
        )
        if not cfg_m.check_only and probe.blocks_full_run:
            print(probe.reason, file=sys.stderr)
            return 2
        result = run_manual_window_analysis(cfg_m, lock_path=lock_path)

    # Prove we only read the lock
    assert_lock_untouched(lock_path)
    print(f"wrote {args.output_dir} report_hash={result.report_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

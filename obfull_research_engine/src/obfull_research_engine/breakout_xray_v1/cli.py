"""CLI for Breakout X-Ray V1."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import EXPLICIT_EXECUTION_REQUIRED, FULL_RUN_BLOCK_REASON
from .execution_hold import DEFAULT_SILVER_LOCK, assert_lock_untouched, read_builder_lock
from .models import EdgeSide, ManualWindowConfig, StrategyEdgeConfig
from .run import run_manual_window_analysis, run_strategy_edge_analysis
from .time_windows import parse_utc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="obfull_research_engine.breakout_xray_v1",
        description="Breakout X-Ray V1 (strategy-edge | manual-window). "
        "Live runs require --execute-live and a free Silver builder lock.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--symbol", required=True)
        sp.add_argument("--local-band-usd", type=float, default=400.0)
        sp.add_argument("--output-dir", required=True)
        sp.add_argument("--check-only", action="store_true")
        sp.add_argument(
            "--execute-live",
            action="store_true",
            help="Explicitly allow live ClickHouse/Bronze adapters. "
            "Never implied by --check-only. Still blocked by active Silver lock.",
        )
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
    execute_live = bool(getattr(args, "execute_live", False))
    check_only = bool(args.check_only)
    output_dir = Path(args.output_dir)

    probe = read_builder_lock(lock_path)
    print(
        f"lock exists={probe.exists} pid={probe.pid} alive={probe.pid_alive} "
        f"blocks_full_run={probe.blocks_full_run} execute_live={execute_live} "
        f"check_only={check_only}",
        file=sys.stderr,
    )

    # Dual gate for any non-check full path — never open client here without both.
    if not check_only:
        if not execute_live:
            print(EXPLICIT_EXECUTION_REQUIRED, file=sys.stderr)
            return 2
        if probe.blocks_full_run:
            print(probe.reason or FULL_RUN_BLOCK_REASON, file=sys.stderr)
            return 2

    deps = None
    sentinel = None
    preflight = None
    if not check_only and execute_live:
        from .adapters.live_factory import (
            LiveAnalysisConfig,
            build_live_bundle,
        )

        live_cfg = LiveAnalysisConfig(
            symbol=args.symbol,
            chain_version=(
                "canonical_segment_chain_v1_3_BTCUSDT_20260912T060011Z_f666e592a0bef459"
            ),
            chain_hash=(
                "f666e592a0bef4598545b3f247cf5dd97dd9c53017028e3c33ddfce5e0d15333"
            ),
            output_dir=output_dir,
            local_band_usd=float(args.local_band_usd),
            lock_path=lock_path,
            execute_live=True,
            enable_market_profile=(args.command == "strategy-edge"),
            # Real client only after preflight inside build_live_bundle
            client_factory=None,
        )
        # NOTE: do not call against prod while Silver builder lock is held.
        bundle = build_live_bundle(live_cfg)
        deps = bundle.deps
        sentinel = bundle.sentinel
        preflight = bundle.preflight

    if args.command == "strategy-edge":
        cfg = StrategyEdgeConfig(
            symbol=args.symbol,
            decision_time_utc=parse_utc(args.decision_time_utc),
            edge_side=EdgeSide(args.edge_side),
            pre_window_minutes=args.pre_window_minutes,
            post_window_minutes=args.post_window_minutes,
            local_band_usd=args.local_band_usd,
            output_dir=args.output_dir,
            check_only=check_only,
            reference_price=args.reference_price,
            reference_known_as_of_utc=(
                None
                if not args.reference_known_as_of_utc
                else parse_utc(args.reference_known_as_of_utc)
            ),
        )
        result = run_strategy_edge_analysis(
            cfg,
            deps=deps,
            lock_path=lock_path,
            execute_live=execute_live and not check_only,
            skip_execution_hold=False,
            sentinel=sentinel,
            resource_preflight=preflight,
        )
    else:
        side = EdgeSide(args.reference_side) if args.reference_side else None
        cfg_m = ManualWindowConfig(
            symbol=args.symbol,
            start_utc=parse_utc(args.start_utc),
            end_utc=parse_utc(args.end_utc),
            local_band_usd=args.local_band_usd,
            output_dir=args.output_dir,
            check_only=check_only,
            reference_price=args.reference_price,
            reference_side=side,
            reference_known_as_of_utc=(
                None
                if not args.reference_known_as_of_utc
                else parse_utc(args.reference_known_as_of_utc)
            ),
        )
        result = run_manual_window_analysis(
            cfg_m,
            deps=deps,
            lock_path=lock_path,
            execute_live=execute_live and not check_only,
            skip_execution_hold=False,
            sentinel=sentinel,
            resource_preflight=preflight,
        )

    assert_lock_untouched(lock_path)
    print(f"wrote {args.output_dir} report_hash={result.report_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

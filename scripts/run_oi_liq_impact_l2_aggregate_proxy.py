#!/usr/bin/env python3
"""Run BTC F3 aggregate wall proxy discovery."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from orderbook_analyse.oi_liq_impact_l2.aggregate_proxy.discovery import (  # noqa: E402
    DEFAULT_F1_DIR,
    DEFAULT_F2_DIR,
    DEFAULT_OUTPUT_DIR,
    run_aggregate_proxy_discovery,
)
from orderbook_analyse.oi_liq_impact_l2.aggregate_proxy.loaders import AggregateProxyError  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="BTC F3 aggregate wall proxy discovery (1s aggregates only)."
    )
    parser.add_argument("--f1-dir", type=Path, default=Path(DEFAULT_F1_DIR))
    parser.add_argument("--f2-dir", type=Path, default=Path(DEFAULT_F2_DIR))
    parser.add_argument("--output-dir", type=Path, default=Path(DEFAULT_OUTPUT_DIR))
    parser.add_argument(
        "--smoke-cluster-id",
        default="",
        help="Analyze a single cluster id for smoke validation.",
    )
    parser.add_argument(
        "--max-clusters",
        type=int,
        default=0,
        help="Limit cluster count (0 = all).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    smoke = args.smoke_cluster_id.strip() or None
    max_clusters = args.max_clusters if args.max_clusters > 0 else None
    out_dir = args.output_dir
    if smoke:
        out_dir = out_dir / "smoke"
    try:
        result = run_aggregate_proxy_discovery(
            f1_dir=args.f1_dir,
            f2_dir=args.f2_dir,
            output_dir=out_dir,
            smoke_cluster_id=smoke,
            max_clusters=max_clusters,
        )
    except AggregateProxyError as exc:
        print("BTC_F3_AGGREGATE_WALL_PROXY_BLOCKED", file=sys.stderr)
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print("BTC_F3_AGGREGATE_WALL_PROXY_BLOCKED", file=sys.stderr)
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(result.verdict)
    print(f"output_dir={result.output_dir}")
    print(f"clusters={result.cluster_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

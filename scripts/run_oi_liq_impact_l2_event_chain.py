#!/usr/bin/env python3
"""Run F2 event-chain discovery from frozen F1 artifacts.

This command reads local minute_features.csv and flush_candidates.csv only.
It performs no ClickHouse access, no F1 recomputation and no profitability
analysis.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from orderbook_analyse.oi_liq_impact_l2.event_chain import (  # noqa: E402
    DEFAULT_HORIZON_MINUTES,
    EventChainError,
    run_event_chain_discovery,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Descriptive F2 event-chain discovery from frozen F1 artifacts; "
            "no ClickHouse and no profitability analysis."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        required=True,
        help="Directory containing F1 discovery artifacts.",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--horizon-minutes",
        type=int,
        default=DEFAULT_HORIZON_MINUTES,
        help="Fixed post-flush research horizon; not optimized.",
    )
    parser.add_argument(
        "--no-outcomes",
        action="store_true",
        help="Skip optional event_outcomes_sidecar.csv generation.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_event_chain_discovery(
            input_dir=args.input_dir,
            output_dir=args.output_dir,
            horizon_minutes=args.horizon_minutes,
            include_outcomes=not args.no_outcomes,
        )
    except EventChainError as exc:
        print("BTC_F2_EVENT_CHAIN_DISCOVERY_BLOCKED", file=sys.stderr)
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError) as exc:
        print("BTC_F2_EVENT_CHAIN_DISCOVERY_BLOCKED", file=sys.stderr)
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - controlled CLI boundary
        print("BTC_F2_EVENT_CHAIN_DISCOVERY_BLOCKED", file=sys.stderr)
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print("BTC_F2_EVENT_CHAIN_DISCOVERY_COMPLETE")
    print(
        f"episodes={result.episode_count} "
        f"reclaim={result.summary['stage_counts'].get('PRICE_RECLAIM', 0)} "
        f"output_dir={result.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

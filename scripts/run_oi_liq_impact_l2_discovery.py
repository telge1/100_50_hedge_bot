#!/usr/bin/env python3
"""Run causal OI/liquidation, trade-impact and L2 discovery.

This command writes descriptive features and isolated outcome labels. It does
not generate Strategy Lab trades, optimize thresholds or run a backtest.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from orderbook_analyse.oi_liq_impact_l2.discovery import (  # noqa: E402
    DiscoveryError,
    run_discovery,
)
from orderbook_analyse.oi_liq_impact_l2.discovery_io import (  # noqa: E402
    load_discovery_inputs,
)


def _utc(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must be timezone-aware")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Causal descriptive discovery only; no Strategy Lab trades or "
            "threshold optimization."
        )
    )
    parser.add_argument("--universe", type=Path, required=True)
    parser.add_argument("--start", type=_utc, required=True)
    parser.add_argument("--end", type=_utc, required=True)
    parser.add_argument(
        "--label-horizon-minutes",
        type=int,
        required=True,
        help="Explicit forward-label horizon; labels remain in a separate sidecar.",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--symbol", action="append", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        # Client creation is deliberately confined to the CLI execution path.
        from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client

        result = run_discovery(
            client=get_clickhouse_client(),
            loader=load_discovery_inputs,
            universe_path=args.universe,
            start=args.start,
            end=args.end,
            label_horizon_minutes=args.label_horizon_minutes,
            output_dir=args.output_dir,
            symbols=tuple(args.symbol) if args.symbol else None,
        )
    except (DiscoveryError, OSError, ValueError, KeyError) as exc:
        print("OI_LIQ_IMPACT_L2_DISCOVERY_BLOCKED", file=sys.stderr)
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - controlled CLI boundary
        print("OI_LIQ_IMPACT_L2_DISCOVERY_BLOCKED", file=sys.stderr)
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print("OI_LIQ_IMPACT_L2_DISCOVERY_COMPLETE")
    print(
        f"symbols={result.symbol_count} "
        f"minute_features={result.minute_feature_count} "
        f"candidates={result.candidate_count} "
        f"output_dir={result.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

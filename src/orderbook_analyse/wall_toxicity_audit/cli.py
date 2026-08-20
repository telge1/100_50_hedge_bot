"""CLI for offline wall toxicity audit."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from orderbook_analyse.wall_toxicity_audit.analysis import run_wall_toxicity_audit
from orderbook_analyse.wall_toxicity_audit.types import AUDIT_VERSION, WallToxicityParams


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            f"{AUDIT_VERSION}: offline reconstruction of wall raw lifecycle "
            "(pulling / migration / toxicity scores). Read-only; no trading."
        )
    )
    p.add_argument("--symbol", required=True)
    p.add_argument("--sequence-id", required=True)
    p.add_argument("--wall-sequences-csv", default=None)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--migration-window-ms", type=float, default=2000.0)
    p.add_argument("--migration-qty-tolerance-pct", type=float, default=25.0)
    p.add_argument("--near-market-bps", type=float, default=30.0)
    p.add_argument("--large-pull-min-qty", type=float, default=50_000.0)
    p.add_argument("--large-pull-min-pct", type=float, default=40.0)
    p.add_argument("--neighbor-buckets", type=int, default=2)
    p.add_argument("--remote-min-bps", type=float, default=50.0)
    p.add_argument("--log-level", default="INFO")
    return p


def params_from_args(args: argparse.Namespace) -> WallToxicityParams:
    return WallToxicityParams(
        migration_window_ms=float(args.migration_window_ms),
        migration_qty_tolerance_pct=float(args.migration_qty_tolerance_pct),
        near_market_bps=float(args.near_market_bps),
        large_pull_min_qty=float(args.large_pull_min_qty),
        large_pull_min_pct=float(args.large_pull_min_pct),
        neighbor_buckets=int(args.neighbor_buckets),
        remote_min_bps=float(args.remote_min_bps),
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    csv_path = Path(args.wall_sequences_csv) if args.wall_sequences_csv else None
    bundle = run_wall_toxicity_audit(
        symbol=str(args.symbol).upper(),
        sequence_id=str(args.sequence_id),
        output_dir=Path(args.output_dir),
        params=params_from_args(args),
        wall_sequences_csv=csv_path,
    )
    r = bundle.result
    print(f"=== {AUDIT_VERSION} ===")
    print(f"sequence={bundle.sequence.wall_sequence_id}")
    print(f"classification={r.classification.value}")
    print(f"reliability={r.reliability_score}")
    print(f"toxicity={r.toxicity_score}")
    print(f"spoofing_suspicion={r.spoofing_suspicion.value} (not proof)")
    print(f"migrations={r.migration.migration_event_count}")
    print(f"removed_without_trade_ratio={r.pull.removed_without_trade_ratio}")
    print(f"trades_in_bucket={r.market.trades_in_bucket}")
    print(f"min_distance_bps={r.market.min_distance_bps}")
    print(f"output={Path(args.output_dir).resolve()}")
    print("WALL_TOXICITY_AUDIT_COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

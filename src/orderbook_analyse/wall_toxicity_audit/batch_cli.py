"""CLI for batch wall toxicity + outcome audit."""

from __future__ import annotations

import argparse
import logging
from datetime import datetime
from pathlib import Path

from orderbook_analyse.wall_toxicity_audit.batch import run_wall_toxicity_batch
from orderbook_analyse.wall_toxicity_audit.data_access import parse_utc
from orderbook_analyse.wall_toxicity_audit.types import (
    AUDIT_VERSION,
    DEFAULT_FORWARD_SECONDS,
    OutcomeParams,
    WallToxicityParams,
)


def _parse_forward_seconds(raw: str | None) -> tuple[int, ...]:
    if raw is None or str(raw).strip() == "":
        return DEFAULT_FORWARD_SECONDS
    vals: list[int] = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        v = int(part)
        if v <= 0:
            raise SystemExit(f"forward seconds must be > 0, got {v}")
        vals.append(v)
    if not vals:
        raise SystemExit("empty --forward-seconds")
    return tuple(vals)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            f"{AUDIT_VERSION} batch: toxicity audit + forward wall outcomes "
            "(read-only; no trading)."
        )
    )
    p.add_argument("--symbol", required=True)
    p.add_argument("--wall-sequences-csv", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--start", default=None, help="UTC lower bound on first_seen")
    p.add_argument("--end", default=None, help="UTC upper bound on first_seen")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument(
        "--sequence-status",
        default=None,
        help="Optional filter: CLOSED|OPEN|ALL or substring of end_reason",
    )
    p.add_argument("--migration-window-ms", type=float, default=2000.0)
    p.add_argument("--migration-qty-tolerance-pct", type=float, default=25.0)
    p.add_argument("--near-market-bps", type=float, default=30.0)
    p.add_argument("--touch-bps", type=float, default=5.0)
    p.add_argument("--break-bps", type=float, default=5.0)
    p.add_argument("--large-pull-min-qty", type=float, default=50_000.0)
    p.add_argument("--large-pull-min-pct", type=float, default=40.0)
    p.add_argument(
        "--forward-seconds",
        default=",".join(str(x) for x in DEFAULT_FORWARD_SECONDS),
    )
    p.add_argument("--continue-on-error", action="store_true", default=False)
    p.add_argument("--overwrite", action="store_true", default=False)
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    tox = WallToxicityParams(
        migration_window_ms=float(args.migration_window_ms),
        migration_qty_tolerance_pct=float(args.migration_qty_tolerance_pct),
        near_market_bps=float(args.near_market_bps),
        touch_bps=float(args.touch_bps),
        large_pull_min_qty=float(args.large_pull_min_qty),
        large_pull_min_pct=float(args.large_pull_min_pct),
    )
    outcome = OutcomeParams(
        forward_seconds=_parse_forward_seconds(args.forward_seconds),
        touch_bps=float(args.touch_bps),
        break_bps=float(args.break_bps),
    )
    result = run_wall_toxicity_batch(
        symbol=str(args.symbol).upper(),
        wall_sequences_csv=Path(args.wall_sequences_csv),
        output_dir=Path(args.output_dir),
        toxicity_params=tox,
        outcome_params=outcome,
        start=parse_utc(args.start),
        end=parse_utc(args.end),
        limit=args.limit,
        sequence_status=args.sequence_status,
        continue_on_error=bool(args.continue_on_error),
        overwrite=bool(args.overwrite),
    )
    s = result.summary
    print(f"=== {AUDIT_VERSION} BATCH ===")
    print(f"analyzed={s['n_analyzed']} eligible={s['n_outcome_eligible']} errors={s['n_errors']}")
    print(f"classifications={s['classification_counts']}")
    print(f"data_quality={s['data_quality_counts']}")
    print(f"elapsed_s={s['elapsed_seconds']} maxrss_mb={s['maxrss_mb']}")
    print(f"output={Path(args.output_dir).resolve()}")
    print("WALL_TOXICITY_BATCH_COMPLETE")
    return 0 if s["n_errors"] == 0 or args.continue_on_error else 1


if __name__ == "__main__":
    raise SystemExit(main())

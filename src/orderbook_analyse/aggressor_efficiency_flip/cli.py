"""CLI entry helpers."""

from __future__ import annotations

import argparse
from pathlib import Path

from orderbook_analyse.aggressor_efficiency_flip.runner import run_f0
from orderbook_analyse.aggressor_efficiency_flip.timeutil import parse_utc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "AGGRESSOR_EFFICIENCY_FLIP_DISCOVERY_V1 F0 — research-only dual-impact "
            "discovery. No trades, no profitability, UNFITTED diagnostic profile."
        )
    )
    p.add_argument("--symbol", required=True)
    p.add_argument("--start", required=True, help="UTC start inclusive, e.g. 2026-08-29T08:00:00Z")
    p.add_argument("--end", required=True, help="UTC end exclusive")
    p.add_argument(
        "--profile",
        default="unfitted_f0_diagnostic",
        help="Must contain 'unfitted_f0_diagnostic'",
    )
    p.add_argument("--output-dir", type=Path, required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    start = parse_utc(args.start)
    end = parse_utc(args.end)
    if not (end > start):
        raise SystemExit("end must be > start (end exclusive)")
    out = run_f0(
        symbol=args.symbol,
        start=start,
        end=end,
        output_dir=args.output_dir,
        profile=args.profile,
    )
    print("AEF_F0_OK", out["n_candidates"], "candidates", "funnel=", out["funnel"])
    return 0

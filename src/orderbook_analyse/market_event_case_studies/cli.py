"""CLI for market-event case studies."""

from __future__ import annotations

import argparse
from pathlib import Path

from orderbook_analyse.market_event_case_studies.runner import STUDY_NAME, run_case_studies


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_market_event_case_studies",
        description=(
            "Auto-select pump/dump/reversal cases and write market-event reports. "
            "Research diagnostic only — not a trading signal."
        ),
    )
    p.add_argument(
        "--output-root",
        default="results/market_event_case_studies",
        help="Root directory for study outputs",
    )
    p.add_argument(
        "--trp-root",
        default="/home/telgenbuescher/projects/trading_research_platform",
        help="trading_research_platform path for LLD",
    )
    p.add_argument("--skip-oi-liq", action="store_true")
    p.add_argument(
        "--max-symbols",
        type=int,
        default=None,
        help="Optional limit for smoke tests",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_case_studies(
        output_root=Path(args.output_root),
        trp_root=Path(args.trp_root) if args.trp_root else None,
        skip_oi_liq=bool(args.skip_oi_liq),
        max_symbols=args.max_symbols,
    )
    study_dir = Path(args.output_root) / STUDY_NAME
    print(f"FOUND={summary.get('found_counts')}")
    print(f"SELECTED={summary.get('n_selected')} REPORTS_OK={summary.get('n_reports_ok')}")
    print(f"SUMMARY={study_dir / 'SUMMARY.md'}")
    print(f"INDEX={summary.get('index_csv')}")
    return 0 if summary.get("n_reports_ok", 0) > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())

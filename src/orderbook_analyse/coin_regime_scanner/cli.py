"""CLI for COIN_REGIME_SCANNER_V1."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import DEFAULT_WARMUP_HOURS, SCANNER_VERSION
from .loaders import parse_as_of
from .runner import run_scanner, universe_symbols


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=f"{SCANNER_VERSION}: causal multi-coin regime snapshot (research-only)."
    )
    p.add_argument(
        "--as-of",
        default=None,
        help="UTC timestamp e.g. 2026-08-17T23:59:00Z (default: latest common closed 1m)",
    )
    p.add_argument(
        "--warmup-hours",
        type=int,
        default=DEFAULT_WARMUP_HOURS,
        help=f"Warmup lookback hours (default {DEFAULT_WARMUP_HOURS}, min 24)",
    )
    p.add_argument(
        "--output-dir",
        default="results/coin_regime_scanner",
        help="Output directory for JSON/CSV snapshot",
    )
    p.add_argument("--no-csv", action="store_true", help="Skip CSV write")
    p.add_argument(
        "--symbols",
        default=None,
        help="Optional comma-separated subset (default = SYMBOLS_51)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    as_of = parse_as_of(args.as_of) if args.as_of else None
    symbols = None
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = universe_symbols()

    payload = run_scanner(
        as_of=as_of,
        warmup_hours=args.warmup_hours,
        output_dir=Path(args.output_dir),
        write_csv=not args.no_csv,
        symbols=symbols,
    )
    summary = {
        "scanner_version": payload["scanner_version"],
        "as_of": payload["as_of"],
        "n_symbols": payload["n_symbols"],
        "json_path": payload.get("json_path"),
        "csv_path": payload.get("csv_path"),
        "summary": payload.get("summary"),
        "load_errors": payload.get("load_errors"),
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

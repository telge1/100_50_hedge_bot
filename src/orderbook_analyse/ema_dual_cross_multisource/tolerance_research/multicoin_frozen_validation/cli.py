"""CLI for multicoin frozen validation (research-only)."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import Sequence

from .constants import CODE_STATUS, DEFAULT_END, DEFAULT_OUTPUT_DIR, DEFAULT_START, DEFAULT_SYMBOLS_FILE
from .runner import default_cfg, run_backtest, run_dry_run, run_preflight, run_report_only


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_edc_multicoin_frozen_validation",
        description="Causal multi-coin frozen validation of XRP-frozen EDC strategies (research-only).",
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight-only", action="store_true", help="Universe + coverage only; no backtest.")
    mode.add_argument("--run", action="store_true", help="Backtest eligible coins (requires preflight).")
    mode.add_argument("--resume", action="store_true", help="Resume from checkpoints; skip COMPLETE coins.")
    mode.add_argument("--report-only", action="store_true", help="Rebuild reports from checkpoints; no market reload.")
    mode.add_argument("--dry-run", action="store_true", help="Validate config / write plan; no ClickHouse queries.")

    p.add_argument("--symbols-file", default=DEFAULT_SYMBOLS_FILE)
    p.add_argument("--start", default=DEFAULT_START.isoformat().replace("+00:00", "Z"), type=str)
    p.add_argument("--end", default=DEFAULT_END.isoformat().replace("+00:00", "Z"), type=str)
    p.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--max-workers", type=int, default=1, help="Keep low to avoid stressing live collectors/CH.")
    p.add_argument("--checkpoint-every", type=int, default=1)
    p.add_argument("--limit-symbols", type=int, default=None, help="Optional prefix limit (not performance-based).")
    return p


def cfg_from_args(args: argparse.Namespace, argv: Sequence[str]) -> dict:
    if args.run and args.resume:
        raise SystemExit("--run and --resume are mutually exclusive")
    cfg = default_cfg(
        symbols_file=args.symbols_file,
        start=_parse_dt(args.start),
        end=_parse_dt(args.end),
        output_dir=args.output_dir,
        max_workers=args.max_workers,
        checkpoint_every=args.checkpoint_every,
        limit_symbols=args.limit_symbols,
        cli_argv=list(argv),
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    return cfg


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = cfg_from_args(args, argv)

    if args.dry_run:
        result = run_dry_run(cfg)
    elif args.preflight_only:
        result = run_preflight(cfg)
    elif args.run:
        result = run_backtest(cfg, resume=False)
    elif args.resume:
        result = run_backtest(cfg, resume=True)
    elif args.report_only:
        result = run_report_only(cfg)
    else:
        parser.error("No mode selected")
        return 2

    print("output_dir:", result.get("output_dir"))
    print("verdict:", result.get("verdict") or result.get("plan", {}).get("code_status") or CODE_STATUS)
    if args.dry_run:
        print("clickhouse_queries: false")
        print("code_status:", CODE_STATUS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""CLI for multicoin frozen reference feature enrichment (research-only)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any, Sequence

from . import constants as C
from .runner import default_cfg, run_analyze, run_dry_run, run_enrich, run_report_only


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_edc_multicoin_reference_enrichment",
        description="Causal feature enrichment for frozen multi-coin EDC reference cell (research-only).",
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Plan + write specs; no database.")
    mode.add_argument("--enrich", action="store_true", help="Enrich candidates from market DB.")
    mode.add_argument("--analyze", action="store_true", help="Analyze enriched files only; no market DB.")
    mode.add_argument("--report-only", action="store_true", help="Aggregate existing analysis artifacts only.")

    p.add_argument("--input-dir", default=C.DEFAULT_INPUT_DIR)
    p.add_argument("--output-dir", default=C.DEFAULT_OUTPUT_DIR)
    p.add_argument("--start", default=C.DEFAULT_START.isoformat().replace("+00:00", "Z"), type=str)
    p.add_argument("--end", default=C.DEFAULT_END.isoformat().replace("+00:00", "Z"), type=str)
    p.add_argument("--max-workers", type=int, default=1)
    p.add_argument("--limit-symbols", type=int, default=None)
    p.add_argument("--symbols", nargs="*", default=None)
    p.add_argument("--checkpoint-every", type=int, default=1)
    return p


def _print_result(result: dict[str, Any], *, dry_run: bool = False) -> None:
    keys = [
        "reference_input_path",
        "reference_rows_before_filter",
        "reference_rows_after_filter",
        "unique_candidate_ids",
        "symbols_total",
        "symbols_completed",
        "symbols_resumed",
        "symbols_failed",
        "enriched_rows",
        "output_files",
        "clickhouse_query_count",
        "output_dir",
        "verdict",
    ]
    for k in keys:
        if k in result and result[k] is not None:
            v = result[k]
            if k == "output_files" and isinstance(v, list):
                print(f"{k}: {json.dumps(v)}")
            else:
                print(f"{k}: {v}")
    # Always print these
    print("clickhouse_queries:", result.get("clickhouse_queries"))
    if "verdict" not in result or result.get("verdict") is None:
        print("verdict:", result.get("verdict"))
    if dry_run:
        print("code_status:", C.CODE_STATUS)


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = default_cfg(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        start=_parse_dt(args.start),
        end=_parse_dt(args.end),
        max_workers=args.max_workers,
        limit_symbols=args.limit_symbols,
        symbols=args.symbols,
        checkpoint_every=args.checkpoint_every,
        cli_argv=list(argv),
    )

    if args.dry_run:
        result = run_dry_run(cfg)
        _print_result(result, dry_run=True)
        return int(result.get("exit_code", 0))

    if args.enrich:
        result = run_enrich(cfg)
        _print_result(result)
        # CODE_READY must never be the enrich end verdict
        if result.get("verdict") == C.CODE_STATUS:
            print("error: enrich returned CODE_READY (invalid)", file=sys.stderr)
            return 1
        return int(result.get("exit_code", 1 if result.get("verdict") != C.STATUS_COMPLETE else 0))

    if args.analyze:
        result = run_analyze(cfg)
        _print_result(result)
        return int(result.get("exit_code", 0))

    if args.report_only:
        result = run_report_only(cfg)
        _print_result(result)
        return int(result.get("exit_code", 0))

    parser.error("No mode selected")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

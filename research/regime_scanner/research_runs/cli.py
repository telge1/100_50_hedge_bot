"""CLI for research-run schema, baseline execution, and comparison."""

from __future__ import annotations

import argparse
import json
import sys

from research.regime_scanner.candle_sources import load_regime_db_env_file
from research.regime_scanner.mysql_candle_store.config import load_regime_db_config
from research.regime_scanner.research_runs.baseline_runner import run_baseline_research
from research.regime_scanner.research_runs.compare import compare_runs
from research.regime_scanner.research_runs.store_mysql import MySQLResearchStore


def _open_store() -> MySQLResearchStore:
    load_regime_db_env_file()
    config = load_regime_db_config()
    return MySQLResearchStore(config)


def cmd_init_schema(_: argparse.Namespace) -> int:
    store = _open_store()
    try:
        store.init_schema()
        print("Research schema initialized (idempotent).")
        return 0
    finally:
        store.close()


def cmd_run_baseline(args: argparse.Namespace) -> int:
    store = _open_store()
    candles_before = store.count_candles()
    validation_before = store.count_validation_runs()
    try:
        result = run_baseline_research(
            store,
            exchange=args.exchange,
            symbol=args.symbol,
            data_source=args.data_source,
            warmup_start=args.warmup_start,
            start=args.start,
            end=args.end,
            include_pipeline=not args.skip_pipeline,
            pipeline_workers=int(args.pipeline_workers),
        )
    finally:
        candles_after = store.count_candles()
        validation_after = store.count_validation_runs()
        store.close()

    print(json.dumps({k: v for k, v in result.items() if k != "context"}, indent=2, default=str))
    print(f"candles_before={candles_before} candles_after={candles_after}")
    print(f"validation_runs_before={validation_before} validation_runs_after={validation_after}")
    return 0


def cmd_compare_runs(args: argparse.Namespace) -> int:
    store = _open_store()
    try:
        payload = compare_runs(store, args.run_id_a, args.run_id_b)
    finally:
        store.close()
    print(json.dumps(payload, indent=2, default=str))
    return 0 if payload.get("equivalent") else 1


def cmd_show_run(args: argparse.Namespace) -> int:
    store = _open_store()
    try:
        row = store.get_run(args.run_id)
        if row is None:
            print(f"Run not found: {args.run_id}", file=sys.stderr)
            return 1
        print(json.dumps(row, indent=2, default=str))
        if args.sample:
            print("\n--- trend sample ---")
            print(json.dumps(store.load_trend_states(args.run_id)[:3], indent=2, default=str))
            print("\n--- structure sample ---")
            print(json.dumps(store.load_structure_events(args.run_id)[:3], indent=2, default=str))
            print("\n--- signal sample ---")
            print(json.dumps(store.load_signals(args.run_id)[:3], indent=2, default=str))
    finally:
        store.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Regime scanner research runs")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-schema", help="Create research result tables (idempotent)")
    p_init.set_defaults(func=cmd_init_schema)

    p_run = sub.add_parser("run-baseline", help="Execute and store a baseline research run")
    p_run.add_argument("--exchange", default="bybit")
    p_run.add_argument("--symbol", default="APTUSDT")
    p_run.add_argument(
        "--data-source",
        default="mysql",
        choices=["mysql", "feather"],
        help="Default mysql for research runner (scanner default remains feather)",
    )
    p_run.add_argument("--warmup-start", default="2025-12-27T00:00:00Z")
    p_run.add_argument("--start", default="2026-03-01T00:00:00Z")
    p_run.add_argument("--end", default="2026-03-08T00:00:00Z")
    p_run.add_argument(
        "--skip-pipeline",
        action="store_true",
        help="Skip PA/Momentum pipeline (signals marked not_exported)",
    )
    p_run.add_argument("--pipeline-workers", default="1")
    p_run.set_defaults(func=cmd_run_baseline)

    p_cmp = sub.add_parser("compare-runs", help="Compare two stored runs")
    p_cmp.add_argument("--run-id-a", required=True)
    p_cmp.add_argument("--run-id-b", required=True)
    p_cmp.set_defaults(func=cmd_compare_runs)

    p_show = sub.add_parser("show-run", help="Show run metadata")
    p_show.add_argument("--run-id", required=True)
    p_show.add_argument("--sample", action="store_true")
    p_show.set_defaults(func=cmd_show_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

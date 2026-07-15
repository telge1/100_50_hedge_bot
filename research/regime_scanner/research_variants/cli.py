"""CLI for controlled variant research runs."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from research.regime_scanner.candle_sources import load_regime_db_env_file
from research.regime_scanner.mysql_candle_store.config import load_regime_db_config
from research.regime_scanner.research_runs.compare import compare_runs
from research.regime_scanner.research_runs.store_mysql import MySQLResearchStore
from research.regime_scanner.research_variants.runner import (
    repeat_variant,
    run_variant_set,
    verify_baseline_parity,
)
from research.regime_scanner.research_variants.sets import get_variant_set
from research.regime_scanner.research_variants.store_mysql import MySQLVariantStore
from research.regime_scanner.research_variants.window_runner import (
    compare_variant_window_set,
    run_variant_window_set,
)
from research.regime_scanner.research_variants.window_sets import get_window_set
from research.regime_scanner.research_variants.windows import (
    window_hash,
    window_set_hash,
)


def _open_stores() -> tuple[MySQLResearchStore, MySQLVariantStore]:
    load_regime_db_env_file()
    config = load_regime_db_config()
    return MySQLResearchStore(config), MySQLVariantStore(config)


def cmd_init_schema(_: argparse.Namespace) -> int:
    research, variants = _open_stores()
    try:
        research.init_schema()
        variants.init_schema()
        print("Research + variant schema initialized (idempotent).")
        return 0
    finally:
        research.close()
        variants.close()


def cmd_list_variants(args: argparse.Namespace) -> int:
    variant_set = get_variant_set(args.variant_set)
    payload = []
    for v in variant_set.variants:
        payload.append(
            {
                "name": v.name,
                "description": v.description,
                "tags": list(v.tags),
                "parameter_overrides": v.parameter_overrides,
            }
        )
    print(json.dumps(payload, indent=2, default=str))
    return 0


def cmd_run_set(args: argparse.Namespace) -> int:
    variant_set = get_variant_set(args.variant_set)
    research, variants = _open_stores()
    candles_before = research.count_candles()
    validation_before = research.count_validation_runs()
    try:
        research.init_schema()
        variants.init_schema()
        result = run_variant_set(
            research,
            variants,
            variant_set,
            exchange=args.exchange,
            symbol=args.symbol,
            data_source=args.data_source,
            warmup_start=args.warmup_start,
            start=args.start,
            end=args.end,
            skip_pipeline=not args.with_pipeline,
            stop_on_error=not args.continue_on_error,
        )
    finally:
        candles_after = research.count_candles()
        validation_after = research.count_validation_runs()
        research.close()
        variants.close()
    print(json.dumps(result, indent=2, default=str))
    print(f"candles_before={candles_before} candles_after={candles_after}")
    print(f"validation_runs_before={validation_before} validation_runs_after={validation_after}")
    return 0


def cmd_compare_set(args: argparse.Namespace) -> int:
    research, variants = _open_stores()
    try:
        row = variants.get_variant_set_by_name(args.variant_set)
        if row is None:
            print(f"Variant set not found: {args.variant_set}", file=sys.stderr)
            return 1
        runs = variants.list_variant_runs(int(row["id"]))
        print(json.dumps(runs, indent=2, default=str))
    finally:
        research.close()
        variants.close()
    return 0


def cmd_show_variant(args: argparse.Namespace) -> int:
    research, variants = _open_stores()
    try:
        row = variants.get_variant_set_by_name(args.variant_set)
        if row is None:
            print(f"Variant set not found: {args.variant_set}", file=sys.stderr)
            return 1
        runs = variants.list_variant_runs(int(row["id"]))
        match = next((r for r in runs if r.get("variant_name") == args.variant), None)
        if match is None:
            print(f"Variant not found: {args.variant}", file=sys.stderr)
            return 1
        print(json.dumps(match, indent=2, default=str))
        if args.sample and match.get("run_id"):
            print("\n--- trend sample ---")
            print(json.dumps(research.load_trend_states(str(match["run_id"]))[:2], indent=2, default=str))
    finally:
        research.close()
        variants.close()
    return 0


def _metadata_dict(row: dict[str, Any]) -> dict[str, Any]:
    meta = row.get("metadata_json") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            meta = {}
    return meta if isinstance(meta, dict) else {}


def cmd_repeat_best(args: argparse.Namespace) -> int:
    research, variants = _open_stores()
    try:
        row = variants.get_variant_set_by_name(args.variant_set)
        if row is None:
            print(f"Variant set not found: {args.variant_set}", file=sys.stderr)
            return 1
        runs = variants.list_variant_runs(int(row["id"]))
        eligible = [
            r
            for r in runs
            if r.get("status") == "completed"
            and not _metadata_dict(r).get("stability_metrics", {}).get("degenerate")
        ]
        if not eligible:
            print("No eligible non-degenerate completed variants.", file=sys.stderr)
            return 1
        best = sorted(
            eligible,
            key=lambda r: (
                r.get("rank_position") is None,
                r.get("rank_position") or 999,
                r.get("variant_name"),
            ),
        )[0]
        variant_set = get_variant_set(args.variant_set)
        variant = next(v for v in variant_set.variants if v.name == best["variant_name"])
        first_run_id = str(best["run_id"])
        second = repeat_variant(
            research,
            variants,
            variant,
            exchange=args.exchange,
            symbol=args.symbol,
            data_source=args.data_source,
            warmup_start=args.warmup_start,
            start=args.start,
            end=args.end,
            skip_pipeline=not args.with_pipeline,
        )
        cmp = compare_runs(research, first_run_id, str(second["run_id"]))
        payload = {"first_run_id": first_run_id, "repeat": second, "compare": cmp}
        print(json.dumps(payload, indent=2, default=str))
        return 0 if cmp.get("equivalent") else 1
    finally:
        research.close()
        variants.close()


def cmd_list_windows(args: argparse.Namespace) -> int:
    window_set = get_window_set(args.window_set)
    payload = []
    for w in window_set.windows:
        payload.append(
            {
                "name": w.name,
                "description": w.description,
                "warmup_start": w.warmup_start.isoformat(),
                "start_time": w.start_time.isoformat(),
                "end_time": w.end_time.isoformat(),
                "expected_character": w.expected_character,
                "selection_reason": w.selection_reason,
                "window_hash": window_hash(w),
                "evidence": w.evidence,
            }
        )
    print(
        json.dumps(
            {
                "window_set": window_set.name,
                "window_set_hash": window_set_hash(window_set),
                "windows": payload,
            },
            indent=2,
            default=str,
        )
    )
    return 0


def cmd_run_window_set(args: argparse.Namespace) -> int:
    variant_set = get_variant_set(args.variant_set)
    window_set = get_window_set(args.window_set)
    research, variants = _open_stores()
    candles_before = research.count_candles()
    validation_before = research.count_validation_runs()
    try:
        research.init_schema()
        variants.init_schema()
        result = run_variant_window_set(
            research,
            variants,
            variant_set=variant_set,
            window_set=window_set,
            exchange=args.exchange,
            symbol=args.symbol,
            data_source=args.data_source,
            skip_pipeline=not args.with_pipeline,
            pilot=args.pilot,
            reuse_completed=not args.no_reuse,
            stop_on_error=not args.continue_on_error,
        )
    finally:
        candles_after = research.count_candles()
        validation_after = research.count_validation_runs()
        research.close()
        variants.close()
    print(json.dumps(result, indent=2, default=str))
    print(f"candles_before={candles_before} candles_after={candles_after}")
    print(f"validation_runs_before={validation_before} validation_runs_after={validation_after}")
    return 0


def cmd_compare_window_set(args: argparse.Namespace) -> int:
    variant_set = get_variant_set(args.variant_set)
    window_set = get_window_set(args.window_set)
    research, variants = _open_stores()
    try:
        research.init_schema()
        variants.init_schema()
        result = compare_variant_window_set(
            research,
            variants,
            variant_set=variant_set,
            window_set=window_set,
        )
    finally:
        research.close()
        variants.close()
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_resume_window_set(args: argparse.Namespace) -> int:
    args.pilot = False
    args.no_reuse = False
    args.continue_on_error = False
    return cmd_run_window_set(args)


def cmd_evaluate_window_set_from_cache(args: argparse.Namespace) -> int:
    from research.regime_scanner.research_variants.cache_evaluation import (
        evaluate_window_set_from_cache,
    )

    variant_set = get_variant_set(args.variant_set)
    window_set = get_window_set(args.window_set)
    research, variants = _open_stores()
    candles_before = research.count_candles()
    validation_before = research.count_validation_runs()
    try:
        research.init_schema()
        variants.init_schema()
        result = evaluate_window_set_from_cache(
            research,
            variants,
            variant_set=variant_set,
            window_set=window_set,
            exchange=args.exchange,
            symbol=args.symbol,
            data_source=args.data_source,
            rescore_only=args.rescore_only,
        )
    finally:
        candles_after = research.count_candles()
        validation_after = research.count_validation_runs()
        research.close()
        variants.close()
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}, indent=2, default=str))
    print(f"candles_before={candles_before} candles_after={candles_after}")
    print(f"validation_runs_before={validation_before} validation_runs_after={validation_after}")
    return 0


def cmd_audit_state_metrics(args: argparse.Namespace) -> int:
    from research.regime_scanner.research_variants.state_metric_audit import (
        run_state_metric_audit,
    )
    from research.regime_scanner.research_variants.window_store_mysql import MySQLWindowStore

    research, variants = _open_stores()
    try:
        research.init_schema()
        variants.init_schema()
        window_store = MySQLWindowStore(variants)
        summary = run_state_metric_audit(research, variants, window_store)
    finally:
        research.close()
        variants.close()
    print(json.dumps({k: v for k, v in summary.items() if k not in ("reeval_rows",)}, indent=2, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Regime scanner variant runner")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-schema")
    p_init.set_defaults(func=cmd_init_schema)

    p_list = sub.add_parser("list-variants")
    p_list.add_argument("--variant-set", required=True)
    p_list.set_defaults(func=cmd_list_variants)

    p_run = sub.add_parser("run-set")
    p_run.add_argument("--variant-set", required=True)
    p_run.add_argument("--exchange", default="bybit")
    p_run.add_argument("--symbol", default="APTUSDT")
    p_run.add_argument("--data-source", default="mysql", choices=["mysql", "feather"])
    p_run.add_argument("--warmup-start", default="2025-12-27T00:00:00Z")
    p_run.add_argument("--start", default="2026-03-01T00:00:00Z")
    p_run.add_argument("--end", default="2026-03-08T00:00:00Z")
    p_run.add_argument("--with-pipeline", action="store_true", help="Enable PA/Momentum pipeline")
    p_run.add_argument("--continue-on-error", action="store_true")
    p_run.set_defaults(func=cmd_run_set)

    p_cmp = sub.add_parser("compare-set")
    p_cmp.add_argument("--variant-set", required=True)
    p_cmp.set_defaults(func=cmd_compare_set)

    p_show = sub.add_parser("show-variant")
    p_show.add_argument("--variant-set", required=True)
    p_show.add_argument("--variant", required=True)
    p_show.add_argument("--sample", action="store_true")
    p_show.set_defaults(func=cmd_show_variant)

    p_rep = sub.add_parser("repeat-best")
    p_rep.add_argument("--variant-set", required=True)
    p_rep.add_argument("--exchange", default="bybit")
    p_rep.add_argument("--symbol", default="APTUSDT")
    p_rep.add_argument("--data-source", default="mysql", choices=["mysql", "feather"])
    p_rep.add_argument("--warmup-start", default="2025-12-27T00:00:00Z")
    p_rep.add_argument("--start", default="2026-03-01T00:00:00Z")
    p_rep.add_argument("--end", default="2026-03-08T00:00:00Z")
    p_rep.add_argument("--with-pipeline", action="store_true")
    p_rep.set_defaults(func=cmd_repeat_best)

    p_lw = sub.add_parser("list-windows")
    p_lw.add_argument("--window-set", required=True)
    p_lw.set_defaults(func=cmd_list_windows)

    p_rws = sub.add_parser("run-window-set")
    p_rws.add_argument("--variant-set", required=True)
    p_rws.add_argument("--window-set", required=True)
    p_rws.add_argument("--exchange", default="bybit")
    p_rws.add_argument("--symbol", default="APTUSDT")
    p_rws.add_argument("--data-source", default="mysql", choices=["mysql", "feather"])
    p_rws.add_argument("--with-pipeline", action="store_true", help="Enable PA/Momentum pipeline")
    p_rws.add_argument("--pilot", action="store_true", help="Run baseline + slower_confirmation only")
    p_rws.add_argument("--no-reuse", action="store_true", help="Do not reuse completed runs")
    p_rws.add_argument("--continue-on-error", action="store_true")
    p_rws.set_defaults(func=cmd_run_window_set)

    p_cws = sub.add_parser("compare-window-set")
    p_cws.add_argument("--variant-set", required=True)
    p_cws.add_argument("--window-set", required=True)
    p_cws.set_defaults(func=cmd_compare_window_set)

    p_res = sub.add_parser("resume-window-set")
    p_res.add_argument("--variant-set", required=True)
    p_res.add_argument("--window-set", required=True)
    p_res.add_argument("--exchange", default="bybit")
    p_res.add_argument("--symbol", default="APTUSDT")
    p_res.add_argument("--data-source", default="mysql", choices=["mysql", "feather"])
    p_res.add_argument("--with-pipeline", action="store_true")
    p_res.add_argument("--no-reuse", action="store_true")
    p_res.add_argument("--continue-on-error", action="store_true")
    p_res.set_defaults(func=cmd_resume_window_set)

    p_eval = sub.add_parser("evaluate-window-set-from-cache")
    p_eval.add_argument("--variant-set", required=True)
    p_eval.add_argument("--window-set", required=True)
    p_eval.add_argument("--exchange", default="bybit")
    p_eval.add_argument("--symbol", default="APTUSDT")
    p_eval.add_argument("--data-source", default="mysql", choices=["mysql", "feather"])
    p_eval.add_argument(
        "--rescore-only",
        action="store_true",
        help="Re-score from cached timelines only; never runs scanner/indicators/build",
    )
    p_eval.set_defaults(func=cmd_evaluate_window_set_from_cache)

    p_audit = sub.add_parser("audit-state-metrics")
    p_audit.add_argument("--variant-set", required=True)
    p_audit.add_argument("--window-set", required=True)
    p_audit.set_defaults(func=cmd_audit_state_metrics)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

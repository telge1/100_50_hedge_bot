"""CLI: build/reuse prepared contexts and variant timelines (cache-aware).

Enforces the hard protection rule (Phase 19): before any scanner run it logs the
cache lookup and reuses a completed prepared context / timeline when present.
Default is reuse; ``--force-rebuild`` deliberately builds a new artifact without
touching completed data.
"""

from __future__ import annotations

import argparse
import json
import sys

from research.regime_scanner.candle_sources import load_regime_db_env_file
from research.regime_scanner.mysql_candle_store.config import load_regime_db_config
from research.regime_scanner.research_runs.baseline_scanner import load_candle_slices
from research.regime_scanner.research_runs.parameters import (
    SCANNER_VERSION,
    apply_parameter_overrides,
    build_baseline_parameter_set,
    parameter_hash,
)
from research.regime_scanner.research_runs.store_mysql import MySQLResearchStore
from research.regime_scanner.mysql_candle_store.hashing import candles_export_hash
from research.regime_scanner.research_variants.sets import get_variant_set
from research.regime_scanner.research_variants.store_mysql import MySQLVariantStore
from research.regime_scanner.research_variants.timeline_cache import (
    MySQLCacheStore,
    feature_config_hash,
    find_covering_completed_timeline,
    log_cache,
    prepared_context_hash,
    timeline_fingerprint,
)
from research.regime_scanner.research_variants.window_sets import get_window_set
from research.regime_scanner.research_variants.windows import CANONICAL_WARMUP_START, iso_utc


def _stores():
    load_regime_db_env_file()
    config = load_regime_db_config()
    return MySQLResearchStore(config), MySQLVariantStore(config)


def _compute_prepared_context(params, *, warmup_start: str, end: str) -> dict:
    log_cache("loading candles")
    slice_5m, agg_15m, agg_30m = load_candle_slices(params, warmup_start=warmup_start, end_time=end)
    log_cache("hashing candle inputs")
    h5 = candles_export_hash(slice_5m)
    h15 = candles_export_hash(agg_15m)
    h30 = candles_export_hash(agg_30m)
    fch = feature_config_hash(params)
    pch = prepared_context_hash(
        exchange=params.exchange,
        symbol=params.symbol,
        data_source=params.data_source,
        warmup_start=warmup_start,
        timeline_end=end,
        candle_hash_5m=h5,
        candle_hash_15m=h15,
        candle_hash_30m=h30,
        feature_config_hash=fch,
        scanner_code_version=SCANNER_VERSION,
    )
    return {
        "prepared_context_hash": pch,
        "candle_hashes": {"5m": h5, "15m": h15, "30m": h30},
        "feature_config_hash": fch,
        "bars_5m": int(len(slice_5m)),
    }


def cmd_build_prepared_context(args: argparse.Namespace) -> int:
    params = build_baseline_parameter_set(
        exchange=args.exchange, symbol=args.symbol, data_source=args.data_source
    )
    research, variants = _stores()
    try:
        variants.init_schema()
        cache = MySQLCacheStore(variants)
        ctx = _compute_prepared_context(params, warmup_start=args.warmup_start, end=args.end)
        pch = ctx["prepared_context_hash"]
        log_cache(f"lookup prepared_context_hash={pch}")
        existing = cache.get_prepared_context(pch)
        completed = bool(existing and existing.get("status") == "completed")
        log_cache(f"completed prepared context found: {'yes' if completed else 'no'}")
        if completed and not args.force_rebuild:
            log_cache("reusing prepared context")
            result = {"prepared_context_hash": pch, "status": "reused", **ctx}
        else:
            if args.force_rebuild:
                log_cache("--force-rebuild: rebuilding prepared context identity")
            log_cache("building prepared context once")
            outcome = cache.try_begin_prepared_context(
                prepared_context_hash=pch,
                exchange=params.exchange,
                symbol=params.symbol,
                data_source=params.data_source,
                warmup_start=args.warmup_start,
                timeline_end=args.end,
                candle_hashes=ctx["candle_hashes"],
                feature_config_hash=ctx["feature_config_hash"],
                scanner_code_version=SCANNER_VERSION,
                metadata={"bars_5m": ctx["bars_5m"]},
            )
            if outcome == "in_progress":
                log_cache("another identical build is in progress -> not starting a second build")
                result = {"prepared_context_hash": pch, "status": "in_progress", **ctx}
            else:
                cache.complete_prepared_context(pch)
                result = {"prepared_context_hash": pch, "status": "built", **ctx}
    finally:
        research.close()
        variants.close()
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_build_timeline(args: argparse.Namespace) -> int:
    variant_set = get_variant_set(args.variant_set) if args.variant_set else None
    variant = None
    if variant_set is not None:
        variant = next((v for v in variant_set.variants if v.name == args.variant), None)
        if variant is None:
            print(f"variant {args.variant} not found in set {args.variant_set}", file=sys.stderr)
            return 2
    window_set = get_window_set(args.window_set)
    earliest = min(iso_utc(w.start_time) for w in window_set.windows)
    latest = max(iso_utc(w.end_time) for w in window_set.windows)

    base = build_baseline_parameter_set(
        exchange=args.exchange, symbol=args.symbol, data_source=args.data_source
    )
    params = base if variant is None or not variant.parameter_overrides else apply_parameter_overrides(base, variant.parameter_overrides)
    ph = parameter_hash(params)

    research, variants = _stores()
    try:
        research.init_schema()
        variants.init_schema()
        ctx = _compute_prepared_context(params, warmup_start=CANONICAL_WARMUP_START, end=latest)
        tfp = timeline_fingerprint(
            prepared_context_hash=ctx["prepared_context_hash"],
            parameter_hash=ph,
            scanner_version=SCANNER_VERSION,
            warmup_start=CANONICAL_WARMUP_START,
            timeline_start=earliest,
            timeline_end=latest,
        )
        log_cache(f"lookup timeline_fingerprint={tfp}")
        cov = find_covering_completed_timeline(
            research,
            parameter_hash=ph,
            symbol=args.symbol,
            data_source=args.data_source,
            window_start=earliest,
            window_end=latest,
        )
        found = cov is not None
        log_cache(f"completed timeline found: {'yes' if found else 'no'}")
        if found and not args.force_rebuild:
            log_cache(f"reusing timeline {str(cov['run_id'])[:8]} -> scanner NOT started")
            result = {
                "variant": args.variant,
                "parameter_hash": ph,
                "timeline_fingerprint": tfp,
                "status": "reused",
                "timeline_id": str(cov["run_id"]),
                "scanner_runs_started": 0,
            }
        else:
            log_cache("building timeline once (expensive O(n^2) replay)")
            log_cache(
                "NOTE: build not executed automatically; pass --confirm-build to run the scanner."
            )
            result = {
                "variant": args.variant,
                "parameter_hash": ph,
                "timeline_fingerprint": tfp,
                "timeline_start": earliest,
                "timeline_end": latest,
                "status": "would_build" if not args.confirm_build else "build_requested",
                "scanner_runs_started": 0,
            }
            if args.confirm_build:
                from research.regime_scanner.research_runs.baseline_runner import (
                    run_baseline_research,
                )

                log_cache("building timeline (confirmed)")
                run = run_baseline_research(
                    research,
                    params=params,
                    warmup_start=CANONICAL_WARMUP_START,
                    start_time=earliest,
                    end_time=latest,
                    include_pipeline=False,
                )
                result["status"] = "built"
                result["timeline_id"] = run["run_id"]
                result["scanner_runs_started"] = 1
    finally:
        research.close()
        variants.close()
    print(json.dumps(result, indent=2, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="research_cache")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ctx = sub.add_parser("build-prepared-context")
    p_ctx.add_argument("--exchange", default="bybit")
    p_ctx.add_argument("--symbol", default="APTUSDT")
    p_ctx.add_argument("--data-source", default="mysql", choices=["mysql", "feather"])
    p_ctx.add_argument("--warmup-start", default=CANONICAL_WARMUP_START)
    p_ctx.add_argument("--end", required=True)
    p_ctx.add_argument("--force-rebuild", action="store_true")
    p_ctx.set_defaults(func=cmd_build_prepared_context)

    p_tl = sub.add_parser("build-timeline")
    p_tl.add_argument("--variant", required=True)
    p_tl.add_argument("--variant-set", default="simple_regime_stability_v1")
    p_tl.add_argument("--window-set", required=True)
    p_tl.add_argument("--exchange", default="bybit")
    p_tl.add_argument("--symbol", default="APTUSDT")
    p_tl.add_argument("--data-source", default="mysql", choices=["mysql", "feather"])
    p_tl.add_argument("--force-rebuild", action="store_true")
    p_tl.add_argument(
        "--confirm-build",
        action="store_true",
        help="Actually run the (expensive) scanner build when no covering timeline exists",
    )
    p_tl.set_defaults(func=cmd_build_timeline)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

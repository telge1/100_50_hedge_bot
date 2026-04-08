from __future__ import annotations

import argparse
import json
from collections import defaultdict
from statistics import median
from typing import Any, Sequence

from .db import MarketRegimeDBConfig, MarketRegimeStore
from .live_history_analysis import (
    aggregate_horizon_distribution,
    compare_live_history_distributions,
    load_history_rows,
)
from .market_signal_engine import MarketSignalEngine
from .profile_builder import ProfileBuilder
from .profile_updater import ProfileUpdater


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Market regime profile tooling.")
    parser.add_argument("--db-host", default=None)
    parser.add_argument("--db-port", type=int, default=None)
    parser.add_argument("--db-user", default=None)
    parser.add_argument("--db-password", default=None)
    parser.add_argument("--db-name", default=None)

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("ensure-schema", help="Create required market regime tables.")

    build_cmd = subparsers.add_parser("build-profiles", help="Build fresh profiles from history.")
    build_cmd.add_argument("--symbols", default=None, help="Comma-separated symbol list.")
    build_cmd.add_argument("--rolling-days", type=int, default=None)
    build_cmd.add_argument("--no-history", action="store_true")

    update_cmd = subparsers.add_parser("update-profiles", help="Refresh profiles for symbols.")
    update_cmd.add_argument("--symbols", default=None, help="Comma-separated symbol list.")
    update_cmd.add_argument("--rolling-days", type=int, default=None)
    update_cmd.add_argument("--no-history", action="store_true")

    live_cmd = subparsers.add_parser("run-live-signal", help="Process recent raw rows through the live engine.")
    live_cmd.add_argument("--symbols", default=None, help="Comma-separated symbol list.")
    live_cmd.add_argument("--limit", type=int, default=5, help="Limit active symbols when --symbols is omitted.")
    live_cmd.add_argument("--no-persist", action="store_true")

    debug_cmd = subparsers.add_parser("debug-live-state", help="Print recent persisted live state rows.")
    debug_cmd.add_argument("--symbols", default=None, help="Comma-separated symbol list.")
    debug_cmd.add_argument("--limit", type=int, default=20, help="Maximum rows to return.")

    backfill_state_cmd = subparsers.add_parser(
        "backfill-oi-price-state",
        help="Backfill oi_price_state and deterministic OI-price flags from raw history.",
    )
    backfill_state_cmd.add_argument("--dry-run", action="store_true", help="Report what would change without updating rows.")
    backfill_state_cmd.add_argument("--batch-size", type=int, default=1000, help="Rows per batch during backfill.")

    backfill_cmd = subparsers.add_parser(
        "backfill-oi-price-flags",
        help="Backfill deterministic OI-price one-hot flags from oi_price_state.",
    )
    backfill_cmd.add_argument("--dry-run", action="store_true", help="Report what would change without updating rows.")

    analyze_cmd = subparsers.add_parser(
        "analyze-oi-price-performance",
        help="Analyze historical forward returns by oi_price_state and state context.",
    )
    analyze_cmd.add_argument("--symbols", default=None, help="Comma-separated symbol list.")
    analyze_cmd.add_argument("--min-rows", type=int, default=1, help="Minimum rows required per group.")

    telemetry_cmd = subparsers.add_parser(
        "analyze-live-state",
        help="Summarize historical live-state router telemetry from market_state_live.",
    )
    telemetry_cmd.add_argument("--symbol", default=None, help="Single symbol filter.")
    telemetry_cmd.add_argument("--symbols", default=None, help="Comma-separated symbol list.")
    telemetry_cmd.add_argument("--limit", type=int, default=None, help="Optional limit on newest rows to scan.")

    quality_cmd = subparsers.add_parser(
        "analyze-live-state-quality",
        help="Analyze live-state router quality with forward returns.",
    )
    quality_cmd.add_argument("--symbol", default=None, help="Single symbol filter.")
    quality_cmd.add_argument("--symbols", default=None, help="Comma-separated symbol list.")
    quality_cmd.add_argument("--limit", type=int, default=None, help="Optional limit on newest rows to scan.")

    review_cmd = subparsers.add_parser(
        "review-live-state-quality",
        help="Review live-state quality for dashboarding and reporting.",
    )
    review_cmd.add_argument("--symbol", default=None, help="Single symbol filter.")
    review_cmd.add_argument("--symbols", default=None, help="Comma-separated symbol list.")
    review_cmd.add_argument("--limit", type=int, default=None, help="Optional limit on newest rows to scan.")

    audit_cmd = subparsers.add_parser(
        "audit-data-coverage",
        help="Audit historical table coverage, gaps, joins, and backfill readiness.",
    )
    audit_cmd.add_argument("--symbol", default=None, help="Single symbol filter.")
    audit_cmd.add_argument("--symbols", default=None, help="Comma-separated symbol list.")
    audit_cmd.add_argument("--json", action="store_true", help="Pretty-print JSON output.")

    live_horizon_cmd = subparsers.add_parser(
        "horizon-live-distribution",
        help="Report live horizon coverage (fast/mid/slow) for the latest state rows.",
    )
    live_horizon_cmd.add_argument("--symbols", default=None, help="Comma-separated symbol list.")
    live_horizon_cmd.add_argument("--limit", type=int, default=None, help="Optional limit on newest rows to scan.")

    compare_cmd = subparsers.add_parser(
        "compare-live-history",
        help="Compare live horizon stats with a historical state table.",
    )
    compare_cmd.add_argument("--symbols", default=None, help="Comma-separated symbol list.")
    compare_cmd.add_argument("--limit", type=int, default=None, help="Optional limit on newest rows to scan.")
    compare_cmd.add_argument("--history-table", default=None, help="Name of the historical state table for comparison.")

    validate_cmd = subparsers.add_parser(
        "validate-history-signals",
        help="Materialize signal validation results from market_state_history.",
    )
    validate_cmd.add_argument("--symbol", default=None, help="Single symbol filter.")
    validate_cmd.add_argument("--symbols", default=None, help="Comma-separated symbol list.")
    validate_cmd.add_argument("--limit", type=int, default=None, help="Optional limit on pending signals to process.")

    validation_summary_cmd = subparsers.add_parser(
        "review-signal-validation",
        help="Read aggregated signal validation summary rows.",
    )
    validation_summary_cmd.add_argument("--symbol", default=None, help="Single symbol filter.")
    validation_summary_cmd.add_argument("--symbols", default=None, help="Comma-separated symbol list.")
    validation_summary_cmd.add_argument("--min-count", type=int, default=1, help="Minimum signal count per summary row.")
    validation_summary_cmd.add_argument("--limit", type=int, default=50, help="Maximum summary rows to return.")

    return parser


def _parse_symbols(raw_symbols: str | None) -> list[str] | None:
    if raw_symbols is None:
        return None
    symbols = [symbol.strip().upper() for symbol in raw_symbols.split(",") if symbol.strip()]
    return symbols or None


def _merge_symbol_filters(single_symbol: str | None, raw_symbols: str | None) -> list[str] | None:
    merged: list[str] = []
    if single_symbol and str(single_symbol).strip():
        merged.append(str(single_symbol).strip().upper())
    parsed = _parse_symbols(raw_symbols)
    if parsed:
        merged.extend(parsed)
    deduped = list(dict.fromkeys(merged))
    return deduped or None


def _make_store(args: argparse.Namespace) -> MarketRegimeStore:
    config = MarketRegimeDBConfig()
    if args.db_host:
        config.host = args.db_host
    if args.db_port:
        config.port = args.db_port
    if args.db_user:
        config.user = args.db_user
    if args.db_password is not None:
        config.password = args.db_password
    if args.db_name:
        config.database = args.db_name
    return MarketRegimeStore(config=config)


def _run_live_signal_command(
    store: MarketRegimeStore,
    *,
    symbols: list[str] | None,
    limit: int,
    persist: bool,
) -> dict[str, object]:
    engine = MarketSignalEngine(store)
    target_symbols = symbols or store.load_active_symbols()[: max(limit, 0)]
    results: list[dict[str, object]] = []
    skipped: dict[str, str] = {}

    for symbol in target_symbols:
        snapshots = store.load_recent_raw_snapshots(symbol, limit=2)
        if len(snapshots) < 2:
            skipped[symbol] = "not_enough_raw_rows"
            continue
        previous_state = store.load_latest_state_machine(symbol)
        result = engine.process_symbol(
            symbol,
            snapshots[-1],
            snapshots[-2],
            previous_state,
            persist=persist,
        )
        results.append(
            {
                "symbol": result.symbol,
                "ts": result.ts.isoformat(),
                "state": result.state_machine.current_state if result.state_machine else None,
                "slow_state": result.slow_regime.state if result.slow_regime else None,
                "mid_state": result.mid_regime.state if result.mid_regime else None,
                "mid_debug": result.mid_regime.debug if result.mid_regime else {},
                "fast_state": result.fast_trigger.state if result.fast_trigger else None,
                "oi_price_state": result.normalized_snapshot.label("oi_price_state", "neutral")
                if result.normalized_snapshot
                else "neutral",
                "oi_price_build_long": bool(result.events.oi_price_build_long) if result.events else False,
                "oi_price_short_covering": bool(result.events.oi_price_short_covering) if result.events else False,
                "oi_price_build_short": bool(result.events.oi_price_build_short) if result.events else False,
                "oi_price_long_unwinding": bool(result.events.oi_price_long_flush) if result.events else False,
                "routed_state": result.routed_regime.routed_state if result.routed_regime else None,
                "confidence": result.routed_regime.confidence if result.routed_regime else None,
                "confidence_source": result.confidence_source,
                "conflict_flags": dict(result.routed_regime.conflict_flags) if result.routed_regime else {},
                "instability_flags": dict(result.routed_regime.instability_flags) if result.routed_regime else {},
                "decision": result.decision,
                "decision_reason": result.decision_reason,
                "entry_allowed": result.entry_allowed,
                "range_unclear_diagnosis": result.range_unclear_diagnosis,
                "transition_reason": result.state_machine.transition_reason if result.state_machine else [],
                "routed_transition_reason": result.routed_regime.transition_reason if result.routed_regime else [],
                "candidate_states": result.regime.candidate_states if result.regime else [],
                "emergency_trigger": bool(result.regime.emergency_trigger) if result.regime else False,
                "persisted": result.persisted,
                "scores": {
                    "fast_pressure": result.fast_trigger.pressure_score_fast if result.fast_trigger else None,
                    "fast_participation": result.fast_trigger.participation_score_fast if result.fast_trigger else None,
                    "fast_instability": result.fast_trigger.instability_score_fast if result.fast_trigger else None,
                    "fast_exhaustion": result.fast_trigger.exhaustion_score_fast if result.fast_trigger else None,
                    "slow_pressure": result.slow_regime.pressure_score_slow if result.slow_regime else None,
                    "slow_participation": result.slow_regime.participation_score_slow if result.slow_regime else None,
                    "slow_exhaustion": result.slow_regime.exhaustion_score_slow if result.slow_regime else None,
                    "slow_transition_counter": result.slow_regime.transition_counter if result.slow_regime else None,
                    "slow_bias": result.slow_regime.bias if result.slow_regime else None,
                    "mid_state_present": result.mid_regime.state is not None if result.mid_regime else False,
                },
            }
        )

    return {
        "ok": True,
        "command": "run-live-signal",
        "processed_count": len(results),
        "skipped_symbols": skipped,
        "results": results,
    }


def _run_debug_live_state_command(
    store: MarketRegimeStore,
    *,
    symbols: list[str] | None,
    limit: int,
) -> dict[str, object]:
    rows = store.load_live_state_debug_rows(symbols=symbols, limit=limit)
    serializable_rows = [
        {
            "symbol": row["symbol"],
            "ts": row["ts"].isoformat() if row.get("ts") is not None else None,
            "state": row["state"],
            "routed_state": row["routed_state"],
            "slow_state": row["slow_state"],
            "mid_state": row["mid_state"],
            "fast_state": row["fast_state"],
            "confidence": row["confidence"],
            "confidence_source": row["confidence_source"],
            "conflict_flags": row["conflict_flags"],
            "instability_flags": row["instability_flags"],
            "decision": row["decision"],
            "decision_reason": row["decision_reason"],
            "entry_allowed": row["entry_allowed"],
            "range_unclear_diagnosis": row["range_unclear_diagnosis"],
            "transition_reason": row["transition_reason"],
            "routed_transition_reason": row["routed_transition_reason"],
            "oi_price_state": row["oi_price_state"],
            "oi_price_build_long": row["oi_price_build_long"],
            "oi_price_short_covering": row["oi_price_short_covering"],
            "oi_price_build_short": row["oi_price_build_short"],
            "oi_price_long_unwinding": row["oi_price_long_unwinding"],
        }
        for row in rows
    ]
    return {
        "ok": True,
        "command": "debug-live-state",
        "row_count": len(serializable_rows),
        "rows": serializable_rows,
    }


def _run_backfill_oi_price_flags_command(
    store: MarketRegimeStore,
    *,
    dry_run: bool,
) -> dict[str, object]:
    store.ensure_schema()
    summary = store.backfill_market_state_live_oi_price_flags(dry_run=dry_run)
    return {
        "ok": True,
        "command": "backfill-oi-price-flags",
        **summary,
    }


def _run_backfill_oi_price_state_command(
    store: MarketRegimeStore,
    *,
    dry_run: bool,
    batch_size: int,
) -> dict[str, object]:
    store.ensure_schema()
    summary = store.backfill_market_state_live_oi_price_state(
        dry_run=dry_run,
        batch_size=batch_size,
    )
    return {
        "ok": True,
        "command": "backfill-oi-price-state",
        **summary,
    }


def _pct(count: int, total: int) -> float:
    return (count / total * 100.0) if total else 0.0


def _summarize_performance(
    rows: list[dict[str, object]],
    *,
    key_fn,
    min_rows: int,
) -> list[dict[str, object]]:
    grouped: dict[object, dict[str, object]] = defaultdict(
        lambda: {"rows": 0, "returns_5m": [], "returns_15m": []}
    )
    for row in rows:
        group = grouped[key_fn(row)]
        group["rows"] = int(group["rows"]) + 1
        if row.get("future_return_5m") is not None:
            group["returns_5m"].append(float(row["future_return_5m"]))
        if row.get("future_return_15m") is not None:
            group["returns_15m"].append(float(row["future_return_15m"]))

    summary_rows: list[dict[str, object]] = []
    for key, values in grouped.items():
        total_rows = int(values["rows"])
        if total_rows < min_rows:
            continue
        returns_5m = list(values["returns_5m"])
        returns_15m = list(values["returns_15m"])
        summary_rows.append(
            {
                "key": key,
                "rows": total_rows,
                "rows_5m": len(returns_5m),
                "avg_5m": (sum(returns_5m) / len(returns_5m)) if returns_5m else None,
                "median_5m": median(returns_5m) if returns_5m else None,
                "pos_5m_pct": _pct(sum(1 for value in returns_5m if value > 0), len(returns_5m)) if returns_5m else None,
                "neg_5m_pct": _pct(sum(1 for value in returns_5m if value < 0), len(returns_5m)) if returns_5m else None,
                "rows_15m": len(returns_15m),
                "avg_15m": (sum(returns_15m) / len(returns_15m)) if returns_15m else None,
                "median_15m": median(returns_15m) if returns_15m else None,
                "pos_15m_pct": _pct(sum(1 for value in returns_15m if value > 0), len(returns_15m)) if returns_15m else None,
                "neg_15m_pct": _pct(sum(1 for value in returns_15m if value < 0), len(returns_15m)) if returns_15m else None,
            }
        )
    return summary_rows


def _run_analyze_oi_price_performance_command(
    store: MarketRegimeStore,
    *,
    symbols: list[str] | None,
    min_rows: int,
) -> dict[str, object]:
    store.ensure_schema()
    rows = store.load_market_state_live_oi_performance_rows(symbols=symbols)
    distribution: dict[str, int] = defaultdict(int)
    for row in rows:
        distribution[str(row.get("oi_price_state") or "neutral")] += 1

    by_oi_state = _summarize_performance(
        rows,
        key_fn=lambda row: str(row.get("oi_price_state") or "neutral"),
        min_rows=min_rows,
    )
    by_oi_state.sort(key=lambda item: (-int(item["rows"]), str(item["key"])))

    by_state_and_oi = _summarize_performance(
        rows,
        key_fn=lambda row: (
            str(row.get("state") or "range_unclear"),
            str(row.get("oi_price_state") or "neutral"),
        ),
        min_rows=min_rows,
    )
    by_state_and_oi.sort(
        key=lambda item: (-int(item["rows"]), str(item["key"][0]), str(item["key"][1]))
    )

    strongest_weakest: dict[str, object] = {}
    valid_5m = [item for item in by_state_and_oi if item["rows_5m"] and item["avg_5m"] is not None]
    valid_15m = [item for item in by_state_and_oi if item["rows_15m"] and item["avg_15m"] is not None]
    if valid_5m:
        best_5m = max(valid_5m, key=lambda item: float(item["avg_5m"]))
        worst_5m = min(valid_5m, key=lambda item: float(item["avg_5m"]))
        strongest_weakest["best_5m"] = {
            "state": best_5m["key"][0],
            "oi_price_state": best_5m["key"][1],
            "avg_5m": best_5m["avg_5m"],
            "rows_5m": best_5m["rows_5m"],
        }
        strongest_weakest["worst_5m"] = {
            "state": worst_5m["key"][0],
            "oi_price_state": worst_5m["key"][1],
            "avg_5m": worst_5m["avg_5m"],
            "rows_5m": worst_5m["rows_5m"],
        }
    if valid_15m:
        best_15m = max(valid_15m, key=lambda item: float(item["avg_15m"]))
        worst_15m = min(valid_15m, key=lambda item: float(item["avg_15m"]))
        strongest_weakest["best_15m"] = {
            "state": best_15m["key"][0],
            "oi_price_state": best_15m["key"][1],
            "avg_15m": best_15m["avg_15m"],
            "rows_15m": best_15m["rows_15m"],
        }
        strongest_weakest["worst_15m"] = {
            "state": worst_15m["key"][0],
            "oi_price_state": worst_15m["key"][1],
            "avg_15m": worst_15m["avg_15m"],
            "rows_15m": worst_15m["rows_15m"],
        }

    return {
        "ok": True,
        "command": "analyze-oi-price-performance",
        "total_rows_scanned": len(rows),
        "min_rows": int(min_rows),
        "distribution": dict(sorted(distribution.items())),
        "oi_price_state_performance": [
            {
                "oi_price_state": item["key"],
                "rows": item["rows"],
                "rows_5m": item["rows_5m"],
                "avg_5m": item["avg_5m"],
                "median_5m": item["median_5m"],
                "pos_5m_pct": item["pos_5m_pct"],
                "neg_5m_pct": item["neg_5m_pct"],
                "rows_15m": item["rows_15m"],
                "avg_15m": item["avg_15m"],
                "median_15m": item["median_15m"],
                "pos_15m_pct": item["pos_15m_pct"],
                "neg_15m_pct": item["neg_15m_pct"],
            }
            for item in by_oi_state
        ],
        "state_oi_price_state_performance": [
            {
                "state": item["key"][0],
                "oi_price_state": item["key"][1],
                "rows": item["rows"],
                "rows_5m": item["rows_5m"],
                "avg_5m": item["avg_5m"],
                "median_5m": item["median_5m"],
                "pos_5m_pct": item["pos_5m_pct"],
                "neg_5m_pct": item["neg_5m_pct"],
                "rows_15m": item["rows_15m"],
                "avg_15m": item["avg_15m"],
                "median_15m": item["median_15m"],
                "pos_15m_pct": item["pos_15m_pct"],
                "neg_15m_pct": item["neg_15m_pct"],
            }
            for item in by_state_and_oi
        ],
        "strongest_weakest": strongest_weakest,
    }


def _run_audit_data_coverage_command(
    store: MarketRegimeStore,
    *,
    symbols: list[str] | None,
) -> dict[str, object]:
    store.ensure_schema()
    payload = store.audit_data_coverage(symbols=symbols)
    return {
        "ok": True,
        "command": "audit-data-coverage",
        **payload,
    }


def _run_analyze_live_state_command(
    store: MarketRegimeStore,
    *,
    symbols: list[str] | None,
    limit: int | None,
) -> dict[str, object]:
    store.ensure_schema()
    payload = store.analyze_market_state_live_telemetry(
        symbols=symbols,
        limit=limit,
    )
    return {
        "ok": True,
        "command": "analyze-live-state",
        **payload,
    }


def _run_analyze_live_state_quality_command(
    store: MarketRegimeStore,
    *,
    symbols: list[str] | None,
    limit: int | None,
) -> dict[str, object]:
    store.ensure_schema()
    payload = store.analyze_market_state_live_quality(
        symbols=symbols,
        limit=limit,
    )
    return {
        "ok": True,
        "command": "analyze-live-state-quality",
        **payload,
    }


def _run_review_live_state_quality_command(
    store: MarketRegimeStore,
    *,
    symbols: list[str] | None,
    limit: int | None,
) -> dict[str, object]:
    store.ensure_schema()
    payload = store.analyze_market_state_live_quality_review(
        symbols=symbols,
        limit=limit,
    )
    return {
        "ok": True,
        "command": "review-live-state-quality",
        **payload,
    }


def _run_horizon_distribution_command(
    store: MarketRegimeStore,
    *,
    symbols: list[str] | None,
    limit: int | None,
) -> dict[str, Any]:
    rows = store.load_market_state_live_telemetry_rows(symbols=symbols, limit=limit)
    horizons = aggregate_horizon_distribution(rows)
    return {
        "ok": True,
        "command": "horizon-live-distribution",
        "symbols": symbols,
        "limit": limit,
        "total_rows": len(rows),
        "horizons": horizons,
    }


def _run_compare_live_history_command(
    store: MarketRegimeStore,
    *,
    symbols: list[str] | None,
    limit: int | None,
    history_table: str | None,
) -> dict[str, Any]:
    live_rows = store.load_market_state_live_telemetry_rows(symbols=symbols, limit=limit)
    live_stats = aggregate_horizon_distribution(live_rows)
    history_rows, history_error = load_history_rows(
        store,
        history_table,
        symbols=symbols,
        limit=limit,
    )
    history_stats = (
        aggregate_horizon_distribution(history_rows) if history_error is None else None
    )
    comparison = compare_live_history_distributions(live_stats, history_stats)
    return {
        "ok": True,
        "command": "compare-live-history",
        "symbols": symbols,
        "limit": limit,
        "history_table": history_table,
        "history_available": history_error is None,
        "history_error": history_error,
        "total_live_rows": len(live_rows),
        "total_history_rows": len(history_rows),
        "live": {"horizons": live_stats},
        "history": {"horizons": history_stats} if history_error is None else None,
        "comparison": comparison,
    }


def _run_validate_history_signals_command(
    store: MarketRegimeStore,
    *,
    symbols: list[str] | None,
    limit: int | None,
) -> dict[str, Any]:
    store.ensure_schema()
    materialized = store.materialize_market_state_history_signal_validation(
        symbols=symbols,
        limit=limit,
    )
    preview_rows = store.load_signal_validation_summary_rows(
        symbols=symbols,
        min_count=1,
        limit=10,
    )
    serializable_preview = [
        {
            **row,
            "updated_at": row["updated_at"].isoformat() if getattr(row.get("updated_at"), "isoformat", None) else row.get("updated_at"),
        }
        for row in preview_rows
    ]
    return {
        "ok": True,
        "command": "validate-history-signals",
        **materialized,
        "summary_preview": serializable_preview,
    }


def _run_review_signal_validation_command(
    store: MarketRegimeStore,
    *,
    symbols: list[str] | None,
    min_count: int,
    limit: int | None,
) -> dict[str, Any]:
    store.ensure_schema()
    rows = store.load_signal_validation_summary_rows(
        symbols=symbols,
        min_count=min_count,
        limit=limit,
    )
    serializable_rows = [
        {
            **row,
            "updated_at": row["updated_at"].isoformat() if getattr(row.get("updated_at"), "isoformat", None) else row.get("updated_at"),
        }
        for row in rows
    ]
    return {
        "ok": True,
        "command": "review-signal-validation",
        "row_count": len(serializable_rows),
        "rows": serializable_rows,
    }


def run_cli(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = _make_store(args)

    if args.command == "ensure-schema":
        store.ensure_schema()
        print(json.dumps({"ok": True, "command": "ensure-schema"}))
        return 0

    builder = ProfileBuilder(store)
    updater = ProfileUpdater(builder)
    symbols = _parse_symbols(getattr(args, "symbols", None))
    write_history = not bool(getattr(args, "no_history", False))
    symbol_filter = _merge_symbol_filters(getattr(args, "symbol", None), getattr(args, "symbols", None))

    if args.command == "build-profiles":
        result = builder.build_and_persist(
            symbols=symbols,
            rolling_days=args.rolling_days,
            write_history=write_history,
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "command": "build-profiles",
                    "profile_count": len(result.profiles),
                    "skipped_symbols": result.skipped_symbols,
                    "window_start": result.window_start.isoformat(),
                    "window_end": result.window_end.isoformat(),
                },
                sort_keys=True,
            )
        )
        return 0

    if args.command == "update-profiles":
        if symbols is None:
            result = updater.refresh_all_active_symbols(
                rolling_days=args.rolling_days,
                write_history=write_history,
            )
        else:
            result = updater.refresh_symbols(
                symbols=symbols,
                rolling_days=args.rolling_days,
                write_history=write_history,
            )
        print(
            json.dumps(
                {
                    "ok": True,
                    "command": "update-profiles",
                    "refreshed_symbols": result.refreshed_symbols,
                    "skipped_symbols": result.skipped_symbols,
                    "window_start": result.window_start.isoformat(),
                    "window_end": result.window_end.isoformat(),
                },
                sort_keys=True,
            )
        )
        return 0

    if args.command == "run-live-signal":
        payload = _run_live_signal_command(
            store,
            symbols=symbols,
            limit=args.limit,
            persist=not args.no_persist,
        )
        print(json.dumps(payload, sort_keys=True))
        return 0

    if args.command == "debug-live-state":
        payload = _run_debug_live_state_command(
            store,
            symbols=symbols,
            limit=args.limit,
        )
        print(json.dumps(payload, sort_keys=True))
        return 0

    if args.command == "backfill-oi-price-state":
        payload = _run_backfill_oi_price_state_command(
            store,
            dry_run=bool(args.dry_run),
            batch_size=int(args.batch_size),
        )
        print(json.dumps(payload, sort_keys=True))
        return 0

    if args.command == "backfill-oi-price-flags":
        payload = _run_backfill_oi_price_flags_command(
            store,
            dry_run=bool(args.dry_run),
        )
        print(json.dumps(payload, sort_keys=True))
        return 0

    if args.command == "analyze-oi-price-performance":
        payload = _run_analyze_oi_price_performance_command(
            store,
            symbols=symbols,
            min_rows=max(int(args.min_rows), 1),
        )
        print(json.dumps(payload, sort_keys=True))
        return 0

    if args.command == "audit-data-coverage":
        payload = _run_audit_data_coverage_command(
            store,
            symbols=symbol_filter,
        )
        if args.json:
            print(json.dumps(payload, sort_keys=True, indent=2))
        else:
            print(json.dumps(payload, sort_keys=True))
        return 0

    if args.command == "analyze-live-state":
        payload = _run_analyze_live_state_command(
            store,
            symbols=symbol_filter,
            limit=args.limit,
        )
        print(json.dumps(payload, sort_keys=True))
        return 0

    if args.command == "analyze-live-state-quality":
        payload = _run_analyze_live_state_quality_command(
            store,
            symbols=symbol_filter,
            limit=args.limit,
        )
        print(json.dumps(payload, sort_keys=True))
        return 0

    if args.command == "review-live-state-quality":
        payload = _run_review_live_state_quality_command(
            store,
            symbols=symbol_filter,
            limit=args.limit,
        )
        print(json.dumps(payload, sort_keys=True))
        return 0

    if args.command == "horizon-live-distribution":
        payload = _run_horizon_distribution_command(
            store,
            symbols=symbol_filter,
            limit=args.limit,
        )
        print(json.dumps(payload, sort_keys=True))
        return 0

    if args.command == "compare-live-history":
        payload = _run_compare_live_history_command(
            store,
            symbols=symbol_filter,
            limit=args.limit,
            history_table=getattr(args, "history_table", None),
        )
        print(json.dumps(payload, sort_keys=True))
        return 0

    if args.command == "validate-history-signals":
        payload = _run_validate_history_signals_command(
            store,
            symbols=symbol_filter,
            limit=args.limit,
        )
        print(json.dumps(payload, sort_keys=True))
        return 0

    if args.command == "review-signal-validation":
        payload = _run_review_signal_validation_command(
            store,
            symbols=symbol_filter,
            min_count=max(int(args.min_count), 1),
            limit=args.limit,
        )
        print(json.dumps(payload, sort_keys=True))
        return 0

    parser.error(f"Unsupported command: {args.command}")
    return 2


def main() -> int:
    return run_cli()


if __name__ == "__main__":
    raise SystemExit(main())

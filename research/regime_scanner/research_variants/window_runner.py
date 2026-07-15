"""Multi-window variant runner with run reuse."""

from __future__ import annotations

import json
import time
from typing import Any

from research.regime_scanner.research_runs.baseline_runner import run_baseline_research
from research.regime_scanner.research_runs.fingerprint import build_run_fingerprint
from research.regime_scanner.research_runs.git_info import collect_git_info
from research.regime_scanner.research_runs.parameters import (
    assert_baseline_parameter_hash,
    parameter_hash,
)
from research.regime_scanner.research_variants.aggregate import (
    aggregate_variant_results,
    check_window_plausibility,
    window_shares,
)
from research.regime_scanner.research_variants.model import (
    ResearchVariantSet,
    variant_hash,
    variant_set_hash,
    variant_set_json,
)
from research.regime_scanner.research_variants.multiwindow_report import write_multiwindow_report
from research.regime_scanner.research_variants.runner import (
    build_variant_parameters,
    compute_baseline_deltas,
    rank_variants,
    verify_baseline_parity,
)
from research.regime_scanner.research_variants.schema import VARIANT_STATUS_COMPLETED, VARIANT_STATUS_FAILED
from research.regime_scanner.research_variants.stability import (
    compute_stability_metrics,
    stability_metrics_to_run_metrics,
)
from research.regime_scanner.research_variants.windows import (
    ResearchWindow,
    ResearchWindowSet,
    iso_utc,
    window_hash,
    window_set_hash,
    window_set_json,
)
from research.regime_scanner.research_variants.window_store_mysql import MySQLWindowStore
from research.regime_scanner.timeframes import ensure_utc_timestamp

PILOT_VARIANTS = frozenset({"baseline", "slower_confirmation"})
MARCH_WEEK_WINDOW = "transition_march_week"


def _metadata_dict(row: dict[str, Any]) -> dict[str, Any]:
    meta = row.get("metadata_json") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            meta = {}
    return meta if isinstance(meta, dict) else {}


def predict_run_fingerprint(
    params: Any,
    *,
    warmup_start: str,
    start: str,
    end: str,
) -> str:
    from research.regime_scanner.mysql_candle_store.hashing import candles_export_hash
    from research.regime_scanner.research_runs.baseline_scanner import load_candle_slices

    warmup_ts = ensure_utc_timestamp(warmup_start)
    start_ts = ensure_utc_timestamp(start)
    end_ts = ensure_utc_timestamp(end)
    git = collect_git_info()
    code_version = git.commit or params.scanner_version
    slice_5m, agg_15m, agg_30m = load_candle_slices(
        params, warmup_start=warmup_ts, end_time=end_ts
    )
    return build_run_fingerprint(
        params=params,
        start_time=start_ts.to_pydatetime(),
        end_time=end_ts.to_pydatetime(),
        warmup_start=warmup_ts.to_pydatetime(),
        decision_time=None,
        code_version=code_version,
        candle_hash_5m=candles_export_hash(slice_5m),
        candle_hash_15m=candles_export_hash(agg_15m),
        candle_hash_30m=candles_export_hash(agg_30m),
    )


def _select_variants(variant_set: ResearchVariantSet, *, pilot: bool) -> list[Any]:
    if pilot:
        return [v for v in variant_set.variants if v.name in PILOT_VARIANTS]
    return list(variant_set.variants)


def _load_stability_from_run(
    research_store: Any,
    run_id: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    trend = research_store.load_trend_states(run_id)
    structure = research_store.load_structure_events(run_id)
    stability = compute_stability_metrics(trend_states=trend, structure_events=structure)
    run_row = research_store.get_run(run_id)
    return stability, run_row


def _try_reuse_march_variant_run(
    variant_store: Any,
    *,
    variant_set_id: int,
    variant_name: str,
    window: ResearchWindow,
) -> dict[str, Any] | None:
    if window.name != MARCH_WEEK_WINDOW:
        return None
    runs = variant_store.list_variant_runs(variant_set_id)
    match = next(
        (r for r in runs if r.get("variant_name") == variant_name and r.get("status") == "completed"),
        None,
    )
    if match is None or not match.get("run_id"):
        return None
    return match


def run_single_variant_window(
    research_store: Any,
    variant_store: Any,
    window_store: MySQLWindowStore,
    *,
    variant_set: ResearchVariantSet,
    variant_set_id: int,
    window_set_id: int,
    variant: Any,
    window: ResearchWindow,
    exchange: str,
    symbol: str,
    data_source: str,
    skip_pipeline: bool,
    reuse_completed: bool,
) -> dict[str, Any]:
    params = build_variant_parameters(
        variant, exchange=exchange, symbol=symbol, data_source=data_source
    )
    if variant.name == "baseline":
        assert_baseline_parameter_hash(params)
    phash = parameter_hash(params)
    v_hash = variant_hash(variant, resulting_parameter_hash=phash)
    w_hash = window_hash(window)
    warmup = iso_utc(window.warmup_start)
    start = iso_utc(window.start_time)
    end = iso_utc(window.end_time)

    existing = window_store.get_variant_window_run(
        variant_set_id=variant_set_id,
        window_set_id=window_set_id,
        variant_name=variant.name,
        window_name=window.name,
    )
    if existing and existing.get("status") == VARIANT_STATUS_COMPLETED and reuse_completed:
        run_id = str(existing["run_id"])
        stability, run_row = _load_stability_from_run(research_store, run_id)
        return {
            "variant_name": variant.name,
            "window_name": window.name,
            "window_hash": w_hash,
            "variant_hash": v_hash,
            "parameter_hash": phash,
            "run_id": run_id,
            "status": VARIANT_STATUS_COMPLETED,
            "reused": True,
            "reuse_source": "variant_window_run",
            "score": stability.get("score"),
            "degenerate": stability.get("degenerate"),
            "stability_metrics": stability,
            "expected_character": window.expected_character,
            "runtime_seconds": None,
            "hashes": {
                "trend_hash": run_row.get("trend_hash") if run_row else None,
                "structure_hash": run_row.get("structure_hash") if run_row else None,
                "combined_hash": run_row.get("combined_hash") if run_row else None,
            },
        }

    fingerprint = predict_run_fingerprint(
        params, warmup_start=warmup, start=start, end=end
    )
    reused = False
    reuse_source = None
    run_id = ""
    result: dict[str, Any] = {}
    runtime_seconds: float | None = None

    if reuse_completed:
        found = research_store.find_run_by_fingerprint(fingerprint, status="completed")
        if found is not None:
            run_id = str(found["run_id"])
            reused = True
            reuse_source = "run_fingerprint"
        else:
            march = _try_reuse_march_variant_run(
                variant_store,
                variant_set_id=variant_set_id,
                variant_name=variant.name,
                window=window,
            )
            if march is not None:
                candidate_id = str(march["run_id"])
                candidate = research_store.get_run(candidate_id)
                if candidate and candidate.get("run_fingerprint") == fingerprint:
                    run_id = candidate_id
                    reused = True
                    reuse_source = "march_variant_run"

    if reused and run_id:
        stability, run_row = _load_stability_from_run(research_store, run_id)
        row = {
            "variant_name": variant.name,
            "window_name": window.name,
            "window_hash": w_hash,
            "variant_hash": v_hash,
            "parameter_hash": phash,
            "run_id": run_id,
            "status": VARIANT_STATUS_COMPLETED,
            "reused": True,
            "reuse_source": reuse_source,
            "run_fingerprint": fingerprint,
            "score": stability.get("score"),
            "degenerate": stability.get("degenerate"),
            "stability_metrics": stability,
            "expected_character": window.expected_character,
            "runtime_seconds": runtime_seconds,
            "hashes": {
                "trend_hash": run_row.get("trend_hash") if run_row else None,
                "structure_hash": run_row.get("structure_hash") if run_row else None,
                "combined_hash": run_row.get("combined_hash") if run_row else None,
            },
        }
        if variant.name == "baseline":
            if window.name == MARCH_WEEK_WINDOW:
                row["baseline_parity"] = verify_baseline_parity(research_store, run_id)
        window_store.upsert_variant_window_run(
            variant_set_id=variant_set_id,
            window_set_id=window_set_id,
            variant_name=variant.name,
            window_name=window.name,
            variant_hash=v_hash,
            window_hash=w_hash,
            run_id=run_id,
            parameter_hash=phash,
            status=VARIANT_STATUS_COMPLETED,
            score=float(stability.get("score") or 0.0),
            degenerate=bool(stability.get("degenerate")),
            metadata_json={
                "reused": True,
                "reuse_source": reuse_source,
                "run_fingerprint": fingerprint,
                "stability_metrics": stability,
                "expected_character": window.expected_character,
            },
        )
        return row

    t0 = time.perf_counter()
    try:
        result = run_baseline_research(
            research_store,
            exchange=exchange,
            symbol=symbol,
            data_source=data_source,
            warmup_start=warmup,
            start=start,
            end=end,
            include_pipeline=not skip_pipeline,
            params=params,
        )
        run_id = str(result["run_id"])
        runtime_seconds = float(result.get("duration_seconds") or (time.perf_counter() - t0))
        trend = research_store.load_trend_states(run_id)
        structure = research_store.load_structure_events(run_id)
        stability = compute_stability_metrics(trend_states=trend, structure_events=structure)
        variant_store.append_run_metrics(run_id, stability_metrics_to_run_metrics(stability))
        row = {
            "variant_name": variant.name,
            "window_name": window.name,
            "window_hash": w_hash,
            "variant_hash": v_hash,
            "parameter_hash": phash,
            "run_id": run_id,
            "status": VARIANT_STATUS_COMPLETED,
            "reused": False,
            "reuse_source": None,
            "run_fingerprint": fingerprint,
            "score": stability.get("score"),
            "degenerate": stability.get("degenerate"),
            "stability_metrics": stability,
            "expected_character": window.expected_character,
            "runtime_seconds": runtime_seconds,
            "hashes": result.get("hashes"),
        }
        shares = window_shares(stability)
        row["window_shares"] = shares
        if variant.name == "baseline":
            if window.name == MARCH_WEEK_WINDOW:
                row["baseline_parity"] = verify_baseline_parity(research_store, run_id)
            row["plausibility"] = check_window_plausibility(
                expected_character=window.expected_character,
                metrics=stability,
            )
            if window.name == MARCH_WEEK_WINDOW:
                parity = row.get("baseline_parity") or {}
                if not parity.get("equivalent"):
                    raise ValueError(f"baseline parity failed: {parity.get('first_divergence')}")
        window_store.upsert_variant_window_run(
            variant_set_id=variant_set_id,
            window_set_id=window_set_id,
            variant_name=variant.name,
            window_name=window.name,
            variant_hash=v_hash,
            window_hash=w_hash,
            run_id=run_id,
            parameter_hash=phash,
            status=VARIANT_STATUS_COMPLETED,
            score=float(stability.get("score") or 0.0),
            degenerate=bool(stability.get("degenerate")),
            metadata_json={
                "reused": False,
                "run_fingerprint": fingerprint,
                "stability_metrics": stability,
                "expected_character": window.expected_character,
                "window_shares": shares,
            },
        )
        return row
    except Exception as exc:
        window_store.upsert_variant_window_run(
            variant_set_id=variant_set_id,
            window_set_id=window_set_id,
            variant_name=variant.name,
            window_name=window.name,
            variant_hash=v_hash,
            window_hash=w_hash,
            run_id=run_id or "",
            parameter_hash=phash,
            status=VARIANT_STATUS_FAILED,
            score=None,
            degenerate=None,
            metadata_json={"error_type": type(exc).__name__, "error_message": str(exc)},
        )
        raise


def run_variant_window_set(
    research_store: Any,
    variant_store: Any,
    *,
    variant_set: ResearchVariantSet,
    window_set: ResearchWindowSet,
    exchange: str = "bybit",
    symbol: str = "APTUSDT",
    data_source: str = "mysql",
    skip_pipeline: bool = True,
    pilot: bool = False,
    reuse_completed: bool = True,
    stop_on_error: bool = True,
) -> dict[str, Any]:
    window_store = MySQLWindowStore(variant_store)
    vhash = variant_set_hash(variant_set)
    wshash = window_set_hash(window_set)

    variant_set_id = variant_store.ensure_variant_set(
        variant_set_hash=vhash,
        name=variant_set.name,
        description=variant_set.description,
        variants_json=variant_set_json(variant_set),
    )
    window_set_id = window_store.ensure_window_set(
        window_set_hash=wshash,
        name=window_set.name,
        description=window_set.description,
        windows_json=window_set_json(window_set),
    )

    variants = _select_variants(variant_set, pilot=pilot)
    completed: list[dict[str, Any]] = []

    for window in window_set.windows:
        baseline_metrics: dict[str, Any] | None = None
        for variant in variants:
            try:
                row = run_single_variant_window(
                    research_store,
                    variant_store,
                    window_store,
                    variant_set=variant_set,
                    variant_set_id=variant_set_id,
                    window_set_id=window_set_id,
                    variant=variant,
                    window=window,
                    exchange=exchange,
                    symbol=symbol,
                    data_source=data_source,
                    skip_pipeline=skip_pipeline,
                    reuse_completed=reuse_completed,
                )
                if variant.name == "baseline":
                    baseline_metrics = row.get("stability_metrics")
                elif baseline_metrics is not None and row.get("stability_metrics"):
                    row["baseline_deltas"] = compute_baseline_deltas(
                        baseline_metrics, row["stability_metrics"]
                    )
                completed.append(row)
            except Exception:
                if stop_on_error:
                    raise

    # Per-window rankings
    by_window: dict[str, list[dict[str, Any]]] = {}
    for row in completed:
        by_window.setdefault(row["window_name"], []).append(row)
    for wname, rows in by_window.items():
        rankings = rank_variants(rows)
        rank_map = dict(rankings)
        for row in rows:
            row["rank"] = rank_map.get(row["variant_name"])

    # Aggregate per variant
    by_variant: dict[str, list[dict[str, Any]]] = {}
    for row in completed:
        by_variant.setdefault(row["variant_name"], []).append(row)
    aggregates = {
        name: aggregate_variant_results(rows) for name, rows in by_variant.items()
    }

    plausibility: dict[str, Any] = {}
    for window in window_set.windows:
        baseline_row = next(
            (r for r in completed if r["window_name"] == window.name and r["variant_name"] == "baseline"),
            None,
        )
        if baseline_row and baseline_row.get("stability_metrics"):
            plausibility[window.name] = check_window_plausibility(
                expected_character=window.expected_character,
                metrics=baseline_row["stability_metrics"],
            )

    artifacts = write_multiwindow_report(
        variant_set_name=variant_set.name,
        window_set_name=window_set.name,
        window_set=window_set,
        rows=completed,
        aggregates=aggregates,
        plausibility=plausibility,
    )

    return {
        "variant_set": variant_set.name,
        "window_set": window_set.name,
        "variant_set_hash": vhash,
        "window_set_hash": wshash,
        "pilot": pilot,
        "variant_count": len(variants),
        "window_count": len(window_set.windows),
        "runs": completed,
        "aggregates": aggregates,
        "plausibility": plausibility,
        "artifacts": {k: str(v) for k, v in artifacts.items()},
    }


def compare_variant_window_set(
    research_store: Any,
    variant_store: Any,
    *,
    variant_set: ResearchVariantSet,
    window_set: ResearchWindowSet,
) -> dict[str, Any]:
    window_store = MySQLWindowStore(variant_store)
    vs_row = variant_store.get_variant_set_by_name(variant_set.name)
    ws_row = window_store.get_window_set_by_name(window_set.name)
    if vs_row is None or ws_row is None:
        raise ValueError("variant set or window set not found in database")
    stored = window_store.list_variant_window_runs(
        variant_set_id=int(vs_row["id"]),
        window_set_id=int(ws_row["id"]),
    )
    completed: list[dict[str, Any]] = []
    for rec in stored:
        meta = _metadata_dict(rec)
        stability = meta.get("stability_metrics") or {}
        run_id = str(rec.get("run_id") or "")
        run_row = research_store.get_run(run_id) if run_id else None
        completed.append(
            {
                "variant_name": rec["variant_name"],
                "window_name": rec["window_name"],
                "run_id": run_id,
                "status": rec.get("status"),
                "reused": bool(meta.get("reused")),
                "score": rec.get("score"),
                "degenerate": rec.get("degenerate"),
                "stability_metrics": stability,
                "expected_character": meta.get("expected_character"),
                "runtime_seconds": run_row.get("duration_seconds") if run_row else None,
            }
        )

    by_window: dict[str, list[dict[str, Any]]] = {}
    for row in completed:
        by_window.setdefault(row["window_name"], []).append(row)
    for wname, rows in by_window.items():
        rankings = rank_variants(rows)
        rank_map = dict(rankings)
        for row in rows:
            row["rank"] = rank_map.get(row["variant_name"])

    by_variant: dict[str, list[dict[str, Any]]] = {}
    for row in completed:
        by_variant.setdefault(row["variant_name"], []).append(row)
    aggregates = {name: aggregate_variant_results(rows) for name, rows in by_variant.items()}

    plausibility: dict[str, Any] = {}
    for window in window_set.windows:
        baseline_row = next(
            (r for r in completed if r["window_name"] == window.name and r["variant_name"] == "baseline"),
            None,
        )
        if baseline_row and baseline_row.get("stability_metrics"):
            plausibility[window.name] = check_window_plausibility(
                expected_character=window.expected_character,
                metrics=baseline_row["stability_metrics"],
            )

    artifacts = write_multiwindow_report(
        variant_set_name=variant_set.name,
        window_set_name=window_set.name,
        window_set=window_set,
        rows=completed,
        aggregates=aggregates,
        plausibility=plausibility,
    )
    return {
        "variant_set": variant_set.name,
        "window_set": window_set.name,
        "runs": completed,
        "aggregates": aggregates,
        "plausibility": plausibility,
        "artifacts": {k: str(v) for k, v in artifacts.items()},
    }

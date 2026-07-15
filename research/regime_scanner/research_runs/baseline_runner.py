"""Orchestrate reproducible baseline research runs."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Protocol

from research.regime_scanner.research_runs.baseline_scanner import run_baseline_scanner
from research.regime_scanner.research_runs.context import ResearchRunContext
from research.regime_scanner.research_runs.fingerprint import build_run_fingerprint
from research.regime_scanner.research_runs.git_info import collect_git_info
from research.regime_scanner.research_runs.hashing import combined_output_hash
from research.regime_scanner.research_runs.normalize import (
    compute_run_metrics,
    hash_normalized_rows,
    normalize_momentum_events,
    normalize_price_action_events,
    normalize_signals_from_momentum,
    normalize_structure_events,
    normalize_trend_states,
)
from research.regime_scanner.research_runs.parameters import (
    ResearchParameterSet,
    build_baseline_parameter_set,
    parameter_hash,
)
from research.regime_scanner.research_runs.schema import HASH_NOT_EXPORTED
from research.regime_scanner.research_runs.store_memory import InMemoryResearchStore, new_run_id
from research.regime_scanner.timeframes import ensure_utc_timestamp


class ResearchStore(Protocol):
    def init_schema(self) -> None: ...
    def close(self) -> None: ...
    def ensure_parameter_set(
        self, *, parameter_hash: str, scanner_name: str, params: Any
    ) -> int: ...
    def create_running_run(self, row: dict[str, Any]) -> None: ...
    def save_completed_run(
        self,
        *,
        run_id: str,
        updates: dict[str, Any],
        trend_states: list[dict[str, Any]],
        structure_events: list[dict[str, Any]],
        signals: list[dict[str, Any]],
        metrics: list[dict[str, Any]],
    ) -> None: ...
    def mark_failed(self, run_id: str, *, error_type: str, error_message: str) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def run_baseline_research(
    store: ResearchStore,
    *,
    exchange: str = "bybit",
    symbol: str = "APTUSDT",
    data_source: str = "mysql",
    warmup_start: str = "2025-12-27T00:00:00Z",
    start: str = "2026-03-01T00:00:00Z",
    end: str = "2026-03-08T00:00:00Z",
    include_pipeline: bool = True,
    pipeline_workers: int = 1,
    params: ResearchParameterSet | None = None,
) -> dict[str, Any]:
    started = _utcnow()
    run_id = new_run_id()
    params = params or build_baseline_parameter_set(
        exchange=exchange,
        symbol=symbol,
        data_source=data_source,
    )
    phash = parameter_hash(params)
    git = collect_git_info()
    code_version = git.commit or params.scanner_version

    warmup_ts = ensure_utc_timestamp(warmup_start)
    start_ts = ensure_utc_timestamp(start)
    end_ts = ensure_utc_timestamp(end)

    from research.regime_scanner.mysql_candle_store.hashing import candles_export_hash
    from research.regime_scanner.research_runs.baseline_scanner import load_candle_slices

    t_load0 = time.perf_counter()
    slice_5m, agg_15m, agg_30m = load_candle_slices(
        params, warmup_start=warmup_ts, end_time=end_ts
    )
    candle_hash_5m = candles_export_hash(slice_5m)
    candle_hash_15m = candles_export_hash(agg_15m)
    candle_hash_30m = candles_export_hash(agg_30m)
    t_load = time.perf_counter() - t_load0

    fingerprint = build_run_fingerprint(
        params=params,
        start_time=start_ts.to_pydatetime(),
        end_time=end_ts.to_pydatetime(),
        warmup_start=warmup_ts.to_pydatetime(),
        decision_time=None,
        code_version=code_version,
        candle_hash_5m=candle_hash_5m,
        candle_hash_15m=candle_hash_15m,
        candle_hash_30m=candle_hash_30m,
    )

    param_set_id = store.ensure_parameter_set(
        parameter_hash=phash,
        scanner_name=params.scanner_name,
        params=params,
    )

    context = ResearchRunContext(
        run_id=run_id,
        run_fingerprint=fingerprint,
        exchange=params.exchange,
        symbol=params.symbol,
        data_source=params.data_source,
        start_time=start_ts.to_pydatetime(),
        end_time=end_ts.to_pydatetime(),
        warmup_start=warmup_ts.to_pydatetime(),
        decision_time=None,
        parameter_hash=phash,
        git_commit=git.commit,
        git_branch=git.branch,
        working_tree_dirty=git.working_tree_dirty,
    )

    store.create_running_run(
        {
            "run_id": run_id,
            "run_fingerprint": fingerprint,
            "parameter_set_id": param_set_id,
            "exchange": params.exchange,
            "symbol": params.symbol,
            "data_source": params.data_source,
            "start_time": start_ts,
            "end_time": end_ts,
            "warmup_start": warmup_ts,
            "decision_time": None,
            "started_at": started,
            "git_commit": git.commit,
            "git_branch": git.branch,
            "working_tree_dirty": git.working_tree_dirty,
            "candle_hash_5m": candle_hash_5m,
            "candle_hash_15m": candle_hash_15m,
            "candle_hash_30m": candle_hash_30m,
            "metadata_json": {"phase": "baseline", "include_pipeline": include_pipeline},
        }
    )

    try:
        t_scan0 = time.perf_counter()
        scanner = run_baseline_scanner(
            params,
            warmup_start=warmup_ts,
            start_time=start_ts,
            end_time=end_ts,
            include_pipeline=include_pipeline,
            pipeline_workers=pipeline_workers,
        )
        t_scan = time.perf_counter() - t_scan0

        context = ResearchRunContext(
            run_id=run_id,
            run_fingerprint=fingerprint,
            exchange=params.exchange,
            symbol=params.symbol,
            data_source=params.data_source,
            start_time=start_ts.to_pydatetime(),
            end_time=end_ts.to_pydatetime(),
            warmup_start=warmup_ts.to_pydatetime(),
            decision_time=None,
            parameter_hash=phash,
            git_commit=git.commit,
            git_branch=git.branch,
            working_tree_dirty=git.working_tree_dirty,
        )

        t_norm0 = time.perf_counter()
        trend_rows = normalize_trend_states(scanner.trend_snapshots)
        structure_rows = normalize_structure_events(
            scanner.structure_events,
            start_time=start_ts,
            end_time=end_ts,
        )
        if scanner.pipeline_exported and scanner.momentum_confirmations is not None:
            signal_rows = normalize_signals_from_momentum(scanner.momentum_confirmations)
            pa_rows = normalize_price_action_events(scanner.price_action_events or [])
            mom_rows = normalize_momentum_events(scanner.momentum_events or [])
            pa_hash = hash_normalized_rows(pa_rows, key_field="event_key")
            mom_hash = hash_normalized_rows(mom_rows, key_field="event_key")
            signal_hash = hash_normalized_rows(signal_rows, key_field="signal_key")
        else:
            signal_rows = []
            pa_hash = HASH_NOT_EXPORTED
            mom_hash = HASH_NOT_EXPORTED
            signal_hash = HASH_NOT_EXPORTED
        trend_hash = hash_normalized_rows(trend_rows, key_field="event_key")
        structure_hash = hash_normalized_rows(structure_rows, key_field="event_key")
        combined = combined_output_hash(
            trend_state_hash=trend_hash,
            structure_event_hash=structure_hash,
            price_action_hash=pa_hash,
            momentum_hash=mom_hash,
            signal_hash=signal_hash,
        )
        t_norm = time.perf_counter() - t_norm0

        finished = _utcnow()
        duration = (finished - started).total_seconds()
        metrics = compute_run_metrics(
            trend_states=trend_rows,
            structure_events=structure_rows,
            signals=signal_rows,
            runtime_seconds=duration,
        )

        metadata = {
            "timings": {
                "candle_load_seconds": scanner.timings.get("candle_load_seconds", t_load),
                "candle_hash_seconds": scanner.timings.get("candle_hash_seconds"),
                "trend_timeline_seconds": scanner.timings.get("trend_timeline_seconds"),
                "pipeline_seconds": scanner.timings.get("pipeline_seconds"),
                "normalize_seconds": t_norm,
                "scanner_total_seconds": t_scan,
                "total_seconds": duration,
            },
            "include_pipeline": include_pipeline,
            "pipeline_exported": scanner.pipeline_exported,
            "price_action_hash_status": pa_hash,
            "momentum_hash_status": mom_hash,
            "signal_hash_status": signal_hash,
        }

        t_db0 = time.perf_counter()
        store.save_completed_run(
            run_id=run_id,
            updates={
                "run_fingerprint": fingerprint,
                "finished_at": finished,
                "duration_seconds": duration,
                "trend_state_hash": trend_hash,
                "structure_event_hash": structure_hash,
                "price_action_hash": pa_hash,
                "momentum_hash": mom_hash,
                "signal_hash": signal_hash,
                "combined_output_hash": combined,
                "candle_hash_5m": scanner.candle_hash_5m,
                "candle_hash_15m": scanner.candle_hash_15m,
                "candle_hash_30m": scanner.candle_hash_30m,
                "metadata_json": metadata,
            },
            trend_states=trend_rows,
            structure_events=structure_rows,
            signals=signal_rows,
            metrics=metrics,
        )
        t_db = time.perf_counter() - t_db0
        metadata["timings"]["db_write_seconds"] = t_db

        return {
            "run_id": run_id,
            "run_fingerprint": fingerprint,
            "parameter_hash": phash,
            "status": "completed",
            "duration_seconds": duration,
            "counts": {
                "trend_states": len(trend_rows),
                "structure_events": len(structure_rows),
                "signals": len(signal_rows),
            },
            "hashes": {
                "trend_state_hash": trend_hash,
                "structure_event_hash": structure_hash,
                "price_action_hash": pa_hash,
                "momentum_hash": mom_hash,
                "signal_hash": signal_hash,
                "combined_output_hash": combined,
                "candle_hash_5m": scanner.candle_hash_5m,
                "candle_hash_15m": scanner.candle_hash_15m,
                "candle_hash_30m": scanner.candle_hash_30m,
            },
            "context": context,
            "metadata": metadata,
        }
    except Exception as exc:
        store.mark_failed(
            run_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        raise


__all__ = ["InMemoryResearchStore", "run_baseline_research"]

"""Execute baseline scanner outputs without database coupling."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import pandas as pd

from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.mysql_candle_store.hashing import candles_export_hash
from research.regime_scanner.pipeline_audit import run_pipeline_audit
from research.regime_scanner.research_runs.parameters import ResearchParameterSet
from research.regime_scanner.timeframes import aggregate_candles, ensure_utc_timestamp
from research.regime_scanner.trend_state_machine import run_trend_state_timeline


@dataclass
class BaselineScannerResult:
    trend_snapshots: list[Any]
    structure_events: list[Any]
    price_action_events: list[dict[str, Any]] | None
    momentum_events: list[dict[str, Any]] | None
    momentum_confirmations: list[dict[str, Any]] | None
    candle_hash_5m: str
    candle_hash_15m: str
    candle_hash_30m: str
    timings: dict[str, float]
    pipeline_exported: bool


def load_candle_slices(
    params: ResearchParameterSet,
    *,
    warmup_start: object,
    end_time: object,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    warmup_ts = ensure_utc_timestamp(warmup_start)
    end_ts = ensure_utc_timestamp(end_time)
    frame_5m = load_symbol_candles(
        params.symbol,
        data_source=params.data_source,  # type: ignore[arg-type]
    )
    frame_5m["timestamp"] = pd.to_datetime(frame_5m["timestamp"], utc=True)
    slice_5m = frame_5m.loc[
        (frame_5m["timestamp"] >= warmup_ts) & (frame_5m["timestamp"] < end_ts)
    ].copy()
    slice_5m = slice_5m.sort_values("timestamp").reset_index(drop=True)
    agg_15m = aggregate_candles(slice_5m, "15m", end_ts)
    agg_30m = aggregate_candles(slice_5m, "30m", end_ts)
    return slice_5m, agg_15m, agg_30m


def _indicator_window(
    slice_5m: pd.DataFrame,
    *,
    start_time: object,
    warmup_start: object,
    scanner_cfg: Any,
) -> pd.DataFrame:
    """Replay window: causal warm-up before analysis start, floored at warmup_start."""
    start_ts = ensure_utc_timestamp(start_time)
    warmup_ts = ensure_utc_timestamp(warmup_start)
    warm_bars = int(getattr(scanner_cfg, "min_warmup_candles", 400) or 400) + 50
    warm_start = start_ts - pd.Timedelta(minutes=5 * int(warm_bars))
    if warm_start < warmup_ts:
        warm_start = warmup_ts
    return slice_5m.loc[slice_5m["timestamp"] >= warm_start].copy()


def run_baseline_scanner(
    params: ResearchParameterSet,
    *,
    warmup_start: object,
    start_time: object,
    end_time: object,
    include_pipeline: bool = True,
    pipeline_workers: int = 1,
) -> BaselineScannerResult:
    timings: dict[str, float] = {}
    t0 = time.perf_counter()
    slice_5m, agg_15m, agg_30m = load_candle_slices(
        params, warmup_start=warmup_start, end_time=end_time
    )
    timings["candle_load_seconds"] = time.perf_counter() - t0

    t1 = time.perf_counter()
    hash_5m = candles_export_hash(slice_5m)
    hash_15m = candles_export_hash(agg_15m)
    hash_30m = candles_export_hash(agg_30m)
    timings["candle_hash_seconds"] = time.perf_counter() - t1

    t2 = time.perf_counter()
    replay_5m = _indicator_window(
        slice_5m,
        start_time=start_time,
        warmup_start=warmup_start,
        scanner_cfg=params.regime_scanner,
    )
    indicator_frame = compute_indicator_frame(replay_5m, config=params.regime_scanner)
    snapshots, _, structure_events = run_trend_state_timeline(
        indicator_frame,
        cfg=params.trend_state,
        scanner_cfg=params.regime_scanner,
        start_decision_time=start_time,
        end_decision_time=end_time,
    )
    timings["trend_timeline_seconds"] = time.perf_counter() - t2

    pa_events: list[dict[str, Any]] | None = None
    mom_events: list[dict[str, Any]] | None = None
    mom_confirmations: list[dict[str, Any]] | None = None
    pipeline_exported = False

    if include_pipeline:
        t3 = time.perf_counter()
        start_s = ensure_utc_timestamp(start_time).strftime("%Y-%m-%d")
        end_s = ensure_utc_timestamp(end_time).strftime("%Y-%m-%d")
        pipeline = run_pipeline_audit(
            symbol=params.symbol,
            start=start_s,
            end=end_s,
            timeframes=",".join(params.timeframes),
            history_candles=params.history_candles,
            workers=int(pipeline_workers),
            pa_config=params.price_action,
            momentum_config=params.momentum,
            enable_momentum=True,
            scanner_config=params.regime_scanner,
            data_source=params.data_source,  # type: ignore[arg-type]
        )
        timings["pipeline_seconds"] = time.perf_counter() - t3
        pa_events = list(pipeline.get("price_action_events") or [])
        momentum = pipeline.get("momentum") or {}
        mom_events = list(momentum.get("momentum_events") or [])
        mom_confirmations = list(momentum.get("momentum_confirmations") or [])
        pipeline_exported = True

    return BaselineScannerResult(
        trend_snapshots=snapshots,
        structure_events=structure_events,
        price_action_events=pa_events,
        momentum_events=mom_events,
        momentum_confirmations=mom_confirmations,
        candle_hash_5m=hash_5m,
        candle_hash_15m=hash_15m,
        candle_hash_30m=hash_30m,
        timings=timings,
        pipeline_exported=pipeline_exported,
    )

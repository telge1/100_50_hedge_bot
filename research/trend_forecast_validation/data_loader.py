"""Candle loading + data-quality report via existing regime_scanner loaders."""

from __future__ import annotations

from typing import Any

import pandas as pd

from research.regime_scanner.data_loader import (
    REQUIRED_COLUMNS,
    candles_to_dataframe,
    load_symbol_candles,
)
from research.trend_forecast_validation.config import ForecastValidationConfig, parse_utc


def load_apt_5m(cfg: ForecastValidationConfig) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Load closed 5m APTUSDT candles. Prefer MySQL; fall back to feather with note."""
    meta: dict[str, Any] = {
        "requested_data_source": cfg.data_source,
        "exchange": cfg.exchange,
        "symbol": cfg.symbol,
        "timezone": cfg.timezone_name,
        "candle_timestamp_semantics": cfg.candle_timestamp_semantics,
        "loader": "research.regime_scanner.data_loader.load_symbol_candles",
        "fallback_used": False,
        "fallback_reason": None,
    }
    source = str(cfg.data_source or "mysql").strip().lower()
    try:
        frame = load_symbol_candles(
            cfg.symbol,
            data_source=source,
            exchange=cfg.exchange,
            limit=None,
        )
        meta["data_source"] = source
        meta["table_or_path"] = (
            f"mysql:{cfg.exchange}/candles" if source == "mysql" else "feather:DEFAULT_DATA_DIR"
        )
    except Exception as exc:  # noqa: BLE001 — research harness documents fallback
        if source == "mysql":
            frame = load_symbol_candles(
                cfg.symbol,
                data_source="feather",
                exchange=cfg.exchange,
                limit=None,
            )
            meta["data_source"] = "feather"
            meta["fallback_used"] = True
            meta["fallback_reason"] = str(exc)
            meta["table_or_path"] = "feather:DEFAULT_DATA_DIR"
        else:
            raise

    frame = frame.loc[:, list(REQUIRED_COLUMNS)].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    # Closed-candle decision time = open + interval.
    frame["decision_time"] = frame["timestamp"] + pd.Timedelta(minutes=cfg.candle_interval_minutes)
    quality = assess_data_quality(frame, cfg)
    meta.update(quality)
    return frame, meta


def assess_data_quality(frame: pd.DataFrame, cfg: ForecastValidationConfig) -> dict[str, Any]:
    if frame.empty:
        return {
            "n_candles_5m": 0,
            "first_timestamp": None,
            "last_timestamp": None,
            "duplicates": 0,
            "missing_5m_candles": None,
            "largest_gap_minutes": None,
            "gap_count": 0,
        }
    ts = pd.to_datetime(frame["timestamp"], utc=True)
    expected = pd.Timedelta(minutes=cfg.candle_interval_minutes)
    deltas = ts.diff().iloc[1:]
    gaps = deltas[deltas > expected]
    largest = float(gaps.max().total_seconds() / 60.0) if len(gaps) else 0.0
    # Missing count approximation: sum of extra intervals beyond 1 step.
    missing = 0
    for d in gaps:
        steps = int(round(d / expected)) - 1
        if steps > 0:
            missing += steps
    return {
        "n_candles_5m": int(len(frame)),
        "first_timestamp": str(ts.iloc[0]),
        "last_timestamp": str(ts.iloc[-1]),
        "duplicates": 0,  # already de-duplicated
        "missing_5m_candles": int(missing),
        "largest_gap_minutes": largest,
        "gap_count": int(len(gaps)),
        "first_available_vs_configured_warmup_start": {
            "configured_warmup_start": cfg.warmup_start,
            "first_available": str(ts.iloc[0]),
            "deviation_note": (
                "Data begins before/after configured warmup_start; "
                "replay uses max(first_available, warmup_start) as effective start."
                if True
                else None
            ),
        },
    }


def slice_period_masks(
    timestamps: pd.Series,
    cfg: ForecastValidationConfig,
) -> dict[str, pd.Series]:
    """Boolean masks for warmup / development / oos on candle open timestamps."""
    ts = pd.to_datetime(timestamps, utc=True)
    warm_end = pd.Timestamp(parse_utc(cfg.warmup_end))
    dev_start = pd.Timestamp(parse_utc(cfg.development_start))
    dev_end = pd.Timestamp(parse_utc(cfg.development_end))
    oos_start = pd.Timestamp(parse_utc(cfg.out_of_sample_start))
    oos_end = parse_utc(cfg.out_of_sample_end)
    oos_end_ts = pd.Timestamp(oos_end) if oos_end else ts.max()

    # Effective warmup start = max(configured, first data)
    warm_start_cfg = pd.Timestamp(parse_utc(cfg.warmup_start))
    warm_start = max(warm_start_cfg, ts.min())

    return {
        "warmup": (ts >= warm_start) & (ts <= warm_end),
        "development": (ts >= dev_start) & (ts <= dev_end),
        "out_of_sample": (ts >= oos_start) & (ts <= oos_end_ts),
        "effective_warmup_start": warm_start,
    }

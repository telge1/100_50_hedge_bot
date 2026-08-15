"""Causal HTF bar aggregation from closed 5m data only."""

from __future__ import annotations

from typing import Any

import pandas as pd

from research.regime_scanner.market_regime_macro_context_audit import aggregate_closed_htf


def _ts(v: Any) -> pd.Timestamp:
    t = pd.Timestamp(v)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def prepare_5m_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize joined 5m frame for HTF aggregation."""
    out = df.copy()
    if "timestamp" not in out.columns:
        out["timestamp"] = out["bucket_start"]
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    if "volume" not in out.columns:
        if "total_volume" in out.columns:
            out["volume"] = out["total_volume"]
        else:
            out["volume"] = 0.0
    for col in ("open", "high", "low", "close"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.sort_values("timestamp").reset_index(drop=True)


def build_closed_htf_bars(
    candles_5m: pd.DataFrame,
    *,
    minutes: int,
    end_wall: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Return only fully closed HTF buckets.

    Semantics (matches aggregate_closed_htf):
    - timestamp = HTF bucket open (UTC)
    - decision_time = bucket open + minutes = first instant bar is fully closed
    - Incomplete buckets (missing 5m bars or close > end_wall) are omitted
    - Open/forming HTF bars never appear → levels cannot change mid-bar
    """
    frame = prepare_5m_ohlcv(candles_5m)
    if frame.empty:
        return pd.DataFrame()
    wall = _ts(end_wall) if end_wall is not None else _ts(frame["timestamp"].iloc[-1]) + pd.Timedelta(minutes=5)
    # include last 5m bar's close: bucket_start + 5m
    # If timestamps are opens, last bar closes at last_open+5m
    last_close = _ts(frame["timestamp"].iloc[-1]) + pd.Timedelta(minutes=5)
    wall = max(wall, last_close)
    htf = aggregate_closed_htf(frame, int(minutes), wall)
    if htf.empty:
        return htf
    htf = htf.copy()
    htf["timestamp"] = pd.to_datetime(htf["timestamp"], utc=True)
    htf["decision_time"] = pd.to_datetime(htf["decision_time"], utc=True)
    htf["bar_index"] = range(len(htf))
    htf["tf_minutes"] = int(minutes)
    return htf.reset_index(drop=True)


def sequence_segments_5m(df: pd.DataFrame) -> list[pd.DataFrame]:
    """Split 5m frame on sequence_id changes (gap resets)."""
    if df.empty:
        return []
    frame = prepare_5m_ohlcv(df)
    if "sequence_id" not in frame.columns:
        frame["sequence_id"] = 0
    out: list[pd.DataFrame] = []
    for _, g in frame.groupby("sequence_id", sort=True):
        out.append(g.sort_values("timestamp").reset_index(drop=True))
    return out

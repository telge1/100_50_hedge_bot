"""Causal closed-bar prefix helpers for Liquidity Location pools."""

from __future__ import annotations

from typing import Iterable

import pandas as pd

from orderbook_analyse.cluster_sweep_research.clickhouse_source import (
    _timeframe_minutes,
    aggregate_timeframe,
)


def utc_naive(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    return t


def candles_1m_closed_until(df_1m: pd.DataFrame, as_of) -> pd.DataFrame:
    """Keep only fully closed 1m bars: open_time + 1m <= as_of."""
    if df_1m is None or df_1m.empty:
        return df_1m.iloc[0:0].copy() if df_1m is not None else pd.DataFrame()
    t = utc_naive(as_of)
    ot = pd.to_datetime(df_1m["open_time"])
    if getattr(ot.dt, "tz", None) is not None:
        ot = ot.dt.tz_convert("UTC").dt.tz_localize(None)
    return df_1m.loc[ot + pd.Timedelta(minutes=1) <= t].copy()


def build_tf_from_closed_1m_prefix(
    df_1m_prefix: pd.DataFrame,
    timeframes: Iterable[str],
) -> dict[str, pd.DataFrame]:
    """Aggregate TF candles from a causal 1m prefix (incomplete last bucket dropped)."""
    out: dict[str, pd.DataFrame] = {"1m": df_1m_prefix}
    for tf in timeframes:
        out[tf] = aggregate_timeframe(df_1m_prefix, tf)
    return out


def htf_bar_end(open_time, timeframe: str) -> pd.Timestamp:
    return utc_naive(open_time) + pd.Timedelta(minutes=_timeframe_minutes(timeframe))

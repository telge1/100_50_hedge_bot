"""Signal construction — freeze build_symbol_signals / resolve_entries (no MySQL)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from signal_generator.strategy.wave_fade.annotation import annotate_waves_df
from signal_generator.strategy.wave_fade.indicators import attach_indicators
from signal_generator.strategy.wave_fade.parameters import SIGNAL_TFS
from signal_generator.strategy.wave_fade.trend import assign_trend_bucket
from signal_generator.strategy.wave_fade.waves import segment_stoch_waves


def build_waves_from_ohlcv(
    ohlcv: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
) -> pd.DataFrame:
    """attach_indicators → segment_stoch_waves (freeze build_waves_from_db core)."""
    if ohlcv is None or ohlcv.empty:
        return pd.DataFrame()
    ind = attach_indicators(ohlcv)
    waves = segment_stoch_waves(ind)
    if waves.empty:
        return waves
    waves["symbol"] = symbol
    waves["timeframe"] = timeframe
    for c in ("start_available_at", "end_available_at"):
        if c in waves.columns:
            waves[c] = pd.to_datetime(waves[c], utc=True)
    return waves.sort_values("end_available_at").reset_index(drop=True)


def build_symbol_signals(
    symbol: str,
    edges: dict[tuple[str, str, str], dict[float, float]],
    waves_by_tf: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    """All-wave fade events for SIGNAL_TFs with Tier-A / Q4 flags.

    Adapter difference vs freeze: waves are passed in (from HTF OHLCV / 1m
    aggregation) instead of ``build_waves_from_db``. Annotation / Tier-A logic
    is identical.
    """
    rows = []
    for tf in SIGNAL_TFS:
        w = waves_by_tf.get(tf)
        if w is None or w.empty:
            continue
        ann = annotate_waves_df(w, symbol=symbol, timeframe=tf, quantile_edges=edges)
        ann["trend_bucket"] = assign_trend_bucket(ann)
        ann["is_tier_a"] = (ann["trend_bucket"].astype(str) == "TREND_ALIGNED") & (
            ann["eff_quantile"].astype(str) == "Q4"
        )
        ann["is_q4"] = ann["eff_quantile"].astype(str) == "Q4"
        ann["signal_tf"] = tf
        rows.append(ann)
    if not rows:
        return pd.DataFrame()
    df = pd.concat(rows, ignore_index=True)
    df["confirmation_available_at"] = pd.to_datetime(df["confirmation_available_at"], utc=True)
    df = df.sort_values("confirmation_available_at").reset_index(drop=True)
    df["signal_id"] = np.arange(len(df), dtype=np.int64)
    return df


def resolve_entries(
    events: pd.DataFrame, open_times: np.ndarray, opens: np.ndarray
) -> pd.DataFrame:
    """T0 = first 1m open strictly after confirmation_available_at."""
    out = events.copy()
    conf = pd.to_datetime(out["confirmation_available_at"], utc=True).to_numpy(
        dtype="datetime64[ns]"
    )
    idx = np.searchsorted(open_times, conf, side="right").astype(np.int64)
    n = len(open_times)
    valid = (idx >= 0) & (idx < n)
    px = np.full(len(out), np.nan)
    et = np.full(len(out), np.datetime64("NaT", "ns"), dtype="datetime64[ns]")
    px[valid] = opens[idx[valid]]
    et[valid] = open_times[idx[valid]]
    out["entry_i"] = np.where(valid, idx, -1)
    out["entry_price"] = px
    out["entry_time"] = pd.to_datetime(et, utc=True)
    out["entry_valid"] = valid & np.isfinite(px) & (px > 0)
    if len(open_times):
        t0, t1 = open_times[0], open_times[-1]
        et2 = out["entry_time"].to_numpy(dtype="datetime64[ns]")
        out.loc[out["entry_valid"] & ((et2 < t0) | (et2 > t1)), "entry_valid"] = False
    return out

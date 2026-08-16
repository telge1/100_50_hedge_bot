"""Trend bucket classification — freeze assign_trend_bucket only."""

from __future__ import annotations

import pandas as pd


def assign_trend_bucket(df: pd.DataFrame) -> pd.Series:
    """Frozen same-TF EMA mapping (freeze TREND_DOC / assign_trend_bucket)."""
    out = pd.Series("MIXED", index=df.index, dtype=object)
    up = df["direction"].astype(str) == "UP"
    dn = df["direction"].astype(str) == "DOWN"
    bull = df["ema_context"].astype(str) == "EMA_BULL"
    bear = df["ema_context"].astype(str) == "EMA_BEAR"
    out.loc[up & bull] = "TREND_ALIGNED"
    out.loc[dn & bear] = "TREND_ALIGNED"
    out.loc[up & bear] = "COUNTERTREND"
    out.loc[dn & bull] = "COUNTERTREND"
    return out

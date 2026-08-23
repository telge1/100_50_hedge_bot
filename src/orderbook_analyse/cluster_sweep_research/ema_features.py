"""EMA 9 / 20 / 59 on closed candle closes (deterministic SMA-seed EMA)."""

from __future__ import annotations

from typing import Sequence

import numpy as np
import pandas as pd


def ema_series(values: Sequence[float], period: int) -> list[float | None]:
    """Match TRP indicators.ema.ema: SMA seed then recursive k=2/(period+1)."""
    if period < 1:
        raise ValueError("period must be >= 1")
    n = len(values)
    out: list[float | None] = [None] * n
    if n < period:
        return out
    seed = sum(float(v) for v in values[:period]) / period
    out[period - 1] = seed
    k = 2.0 / (period + 1)
    prev = seed
    for i in range(period, n):
        prev = (float(values[i]) - prev) * k + prev
        out[i] = prev
    return out


def attach_emas(
    df: pd.DataFrame,
    *,
    fast: int = 9,
    medium: int = 20,
    slow: int = 59,
    slope_lookbacks: tuple[int, ...] = (1, 3, 5),
) -> pd.DataFrame:
    """Add ema_* columns; requires sorted closed candles with ``close``."""
    out = df.copy()
    closes = out["close"].astype(float).tolist()
    out["ema_9"] = ema_series(closes, fast)
    out["ema_20"] = ema_series(closes, medium)
    out["ema_59"] = ema_series(closes, slow)
    for lb in slope_lookbacks:
        # absolute slope; normalized by price when possible
        e = out["ema_59"]
        raw = e - e.shift(lb)
        out[f"ema_59_slope_{lb}"] = raw
        out[f"ema_59_slope_norm_{lb}"] = raw / out["close"].replace(0, np.nan)
        for name in ("ema_9", "ema_20"):
            s = out[name]
            out[f"{name}_slope_{lb}"] = s - s.shift(lb)
    # structure flags (bull: 9&20 > 59; bear: 9&20 < 59)
    out["ema_bull_stack"] = (out["ema_9"] > out["ema_59"]) & (out["ema_20"] > out["ema_59"])
    out["ema_bear_stack"] = (out["ema_9"] < out["ema_59"]) & (out["ema_20"] < out["ema_59"])
    out["ema_9_20_gap"] = out["ema_9"] - out["ema_20"]
    out["ema_9_59_gap"] = out["ema_9"] - out["ema_59"]
    out["ema_20_59_gap"] = out["ema_20"] - out["ema_59"]
    out["ema_band_width"] = out[["ema_9", "ema_20", "ema_59"]].max(axis=1) - out[
        ["ema_9", "ema_20", "ema_59"]
    ].min(axis=1)
    return out


def required_warmup_bars(slow: int = 59, extra: int = 20) -> int:
    return int(slow) + int(extra)

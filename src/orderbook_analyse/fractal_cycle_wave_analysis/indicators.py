"""Stoch RSI, RSI, CCI, EMA features for fractal wave analysis."""

from __future__ import annotations

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_cycle_wave_analysis import (
    CCI_LENGTH,
    EMA_SPANS,
    RSI_LENGTH,
    STOCH_D_SMOOTH,
    STOCH_HIGH_K,
    STOCH_K_SMOOTH,
    STOCH_LOW_K,
    STOCH_RSI_LENGTH,
)
from orderbook_analyse.mtf_rsi_stoch_audit.indicators import stochastic_rsi, wilder_rsi


def commodity_channel_index(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    length: int = CCI_LENGTH,
) -> pd.Series:
    tp = (high.astype(float) + low.astype(float) + close.astype(float)) / 3.0
    sma = tp.rolling(length, min_periods=length).mean()
    mad = (tp - sma).abs().rolling(length, min_periods=length).mean()
    cci = (tp - sma) / (0.015 * mad.replace(0.0, np.nan))
    return cci


def attach_indicators(ohlcv: pd.DataFrame) -> pd.DataFrame:
    """Causal closed-bar indicators; requires OHLCV + available_at."""
    df = ohlcv.copy()
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)

    rsi = wilder_rsi(close, RSI_LENGTH)
    k, d = stochastic_rsi(
        close,
        rsi_length=RSI_LENGTH,
        stoch_length=STOCH_RSI_LENGTH,
        k_smooth=STOCH_K_SMOOTH,
        d_smooth=STOCH_D_SMOOTH,
    )
    cci = commodity_channel_index(high, low, close, CCI_LENGTH)

    df["rsi"] = rsi
    df["stoch_k"] = k
    df["stoch_d"] = d
    df["cci"] = cci
    df["stoch_bullish_cross"] = (k.shift(1) <= d.shift(1)) & (k > d)
    df["stoch_bearish_cross"] = (k.shift(1) >= d.shift(1)) & (k < d)
    df["stoch_dir"] = np.where(k > d, "UP", np.where(k < d, "DOWN", "FLAT"))
    df["stoch_zone"] = np.where(
        k < STOCH_LOW_K, "LOW", np.where(k > STOCH_HIGH_K, "HIGH", "MID")
    )

    for span in EMA_SPANS:
        df[f"ema{span}"] = close.ewm(span=span, adjust=False, min_periods=span).mean()

    df["price_vs_ema20"] = np.where(
        df["ema20"].notna(),
        np.where(close > df["ema20"], "ABOVE", np.where(close < df["ema20"], "BELOW", "AT")),
        None,
    )
    df["ema9_vs_ema20"] = np.where(
        df["ema9"].notna() & df["ema20"].notna(),
        np.where(
            df["ema9"] > df["ema20"],
            "BULL",
            np.where(df["ema9"] < df["ema20"], "BEAR", "FLAT"),
        ),
        None,
    )
    # Vectorized cycle labels (avoid slow Python loops on multi-million 1m frames).
    delta = k - k.shift(1)
    df["stoch_k_change"] = delta
    df["stoch_state"] = None  # optional; zone already set
    return df

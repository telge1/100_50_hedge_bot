"""RSI / StochRSI / CCI / EMA — freeze logic from fractal_cycle_wave_analysis.

``wilder_rsi`` / ``stochastic_rsi`` were imported by the freeze tree from
``orderbook_analyse.mtf_rsi_stoch_audit.indicators``, but that package is **not**
present in commit f16ae32 (untracked freeze-runtime dependency). The formulas
below are the exact functions that dependency provided at freeze time
(RSI 14 / StochRSI 14/14/3/3), inlined so the port is self-contained.

See FROZEN_BASELINE.md § FREEZE_DEPENDENCY_GAP.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from signal_generator.strategy.wave_fade.parameters import (
    CCI_LENGTH,
    EMA_SPANS,
    RSI_LENGTH,
    STOCH_D_SMOOTH,
    STOCH_HIGH_K,
    STOCH_K_SMOOTH,
    STOCH_LOW_K,
    STOCH_RSI_LENGTH,
)


def wilder_rsi(close: pd.Series, length: int = RSI_LENGTH) -> pd.Series:
    """Classic Wilder RSI; NaN until enough bars."""
    c = close.astype(float)
    delta = c.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
    rsi = rsi.where(~((avg_loss == 0) & (avg_gain == 0)), 50.0)
    return rsi


def stochastic_rsi(
    close: pd.Series,
    *,
    rsi_length: int = RSI_LENGTH,
    stoch_length: int = STOCH_RSI_LENGTH,
    k_smooth: int = STOCH_K_SMOOTH,
    d_smooth: int = STOCH_D_SMOOTH,
) -> tuple[pd.Series, pd.Series]:
    """Stoch RSI %K / %D on Wilder RSI."""
    rsi = wilder_rsi(close, rsi_length)
    lowest = rsi.rolling(stoch_length, min_periods=stoch_length).min()
    highest = rsi.rolling(stoch_length, min_periods=stoch_length).max()
    span = highest - lowest
    raw_k = pd.Series(np.nan, index=rsi.index, dtype=float)
    valid = lowest.notna() & highest.notna() & rsi.notna()
    flat = valid & (span == 0)
    movable = valid & (span > 0)
    raw_k = raw_k.where(~movable, 100.0 * (rsi - lowest) / span)
    raw_k = raw_k.where(~flat, 50.0)
    k = raw_k.rolling(k_smooth, min_periods=k_smooth).mean()
    d = k.rolling(d_smooth, min_periods=d_smooth).mean()
    return k, d


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
    delta = k - k.shift(1)
    df["stoch_k_change"] = delta
    df["stoch_state"] = None
    return df

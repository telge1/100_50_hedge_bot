"""Numpy/pandas indicator helpers for forming-bar 15m estimates."""

from __future__ import annotations

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_cycle_wave_analysis import (
    RSI_LENGTH,
    STOCH_D_SMOOTH,
    STOCH_HIGH_K,
    STOCH_K_SMOOTH,
    STOCH_LOW_K,
    STOCH_RSI_LENGTH,
)
from orderbook_analyse.mtf_rsi_stoch_audit.indicators import stochastic_rsi, wilder_rsi


def stochastic_rsi_last(close: np.ndarray) -> tuple[float, float, float]:
    s = pd.Series(close.astype(float))
    rsi = wilder_rsi(s, RSI_LENGTH)
    k, d = stochastic_rsi(
        s,
        rsi_length=RSI_LENGTH,
        stoch_length=STOCH_RSI_LENGTH,
        k_smooth=STOCH_K_SMOOTH,
        d_smooth=STOCH_D_SMOOTH,
    )
    return float(k.iloc[-1]), float(d.iloc[-1]), float(rsi.iloc[-1])


def ewm_last(close: np.ndarray, span: int) -> float:
    if len(close) == 0:
        return float("nan")
    return float(pd.Series(close.astype(float)).ewm(span=span, adjust=False, min_periods=span).mean().iloc[-1])


def zone_of(k: float) -> str:
    if not np.isfinite(k):
        return "NA"
    if k < STOCH_LOW_K:
        return "LOW"
    if k > STOCH_HIGH_K:
        return "HIGH"
    return "MID"

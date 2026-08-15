"""Causal base features for OI compression breakout (ATR, gaps, orderflow snapshot)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.regime_scanner.liquidation_exhaustion.features import _true_range, _wilder_atr
from research.regime_scanner.oi_compression_breakout.config import ATR_PERIOD, ATR_PCT_LOOKBACK, BAR_SECONDS


def contiguous_same_sequence(seq: np.ndarray, ts: np.ndarray, i0: int, i1: int) -> bool:
    """True if indices [i0, i1] inclusive are same sequence and adjacent 5m bars."""
    if i0 < 0 or i1 >= len(seq) or i0 > i1:
        return False
    s0 = seq[i0]
    for i in range(i0, i1 + 1):
        if seq[i] != s0:
            return False
        if i > i0:
            dt = (pd.Timestamp(ts[i]) - pd.Timestamp(ts[i - 1])).total_seconds()
            if dt != BAR_SECONDS:
                return False
    return True


def enrich_symbol_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Add ATR14 and causal ATR percentile; require OHLCV + OI columns."""
    out = df.copy()
    high = out["high"].to_numpy(dtype=float)
    low = out["low"].to_numpy(dtype=float)
    close = out["close"].to_numpy(dtype=float)
    prev = np.roll(close, 1)
    prev[0] = close[0]
    tr = _true_range(high, low, prev)
    atr = _wilder_atr(tr, ATR_PERIOD)
    out["atr_14"] = atr
    out["true_range"] = tr

    # causal ATR percentile rank vs prior ATR_PCT_LOOKBACK bars (exclude current)
    n = len(atr)
    atr_pctl = np.full(n, np.nan)
    buf: list[float] = []
    seq = out["sequence_id"].to_numpy()
    ts = out["bucket_start"].to_numpy()
    for i in range(n):
        if len(buf) >= 20:
            # percentile rank of prior ATR distribution
            atr_pctl[i] = float(np.mean(np.asarray(buf[-ATR_PCT_LOOKBACK:]) <= atr[i]) * 100.0) if np.isfinite(atr[i]) else np.nan
        # reset buffer on gap
        if i > 0:
            dt = (pd.Timestamp(ts[i]) - pd.Timestamp(ts[i - 1])).total_seconds()
            if dt != BAR_SECONDS or seq[i] != seq[i - 1]:
                buf = []
        if np.isfinite(atr[i]):
            buf.append(float(atr[i]))
    out["atr_14_pctl_288"] = atr_pctl
    return out

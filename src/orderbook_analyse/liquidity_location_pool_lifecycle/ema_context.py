"""Closed-bar EMA 9/20/59/200 + ATR helpers."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.cluster_sweep_research.ema_features import ema_series

from .constants import ATR_PERIOD, EMA_PERIODS, SWING_LOOKBACK


def attach_context(df: pd.DataFrame) -> pd.DataFrame:
    """Add EMAs, ATR, and causal swing high/low on closed bars."""
    out = df.sort_values("open_time").reset_index(drop=True).copy()
    out["open_time"] = pd.to_datetime(out["open_time"])
    closes = out["close"].astype(float).tolist()
    for p in EMA_PERIODS:
        out[f"ema_{p}"] = ema_series(closes, p)
        out[f"ema_{p}_slope_1"] = out[f"ema_{p}"] - out[f"ema_{p}"].shift(1)

    high = out["high"].astype(float)
    low = out["low"].astype(float)
    close = out["close"].astype(float)
    prev = close.shift(1)
    tr = pd.concat([(high - low), (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    out["atr_14"] = tr.rolling(ATR_PERIOD, min_periods=ATR_PERIOD).mean()

    # Causal prior swing: max high / min low over lookback ending at prior bar
    lb = SWING_LOOKBACK
    out["prior_swing_high"] = high.shift(1).rolling(lb, min_periods=lb).max()
    out["prior_swing_low"] = low.shift(1).rolling(lb, min_periods=lb).min()
    return out


def ema_snapshot(
    row: pd.Series,
    *,
    pool_lower: float,
    pool_upper: float,
    label: str,
) -> dict[str, Any]:
    px = float(row["close"])
    emas = {}
    for p in EMA_PERIODS:
        v = row.get(f"ema_{p}")
        emas[p] = None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)

    above = {p: (None if emas[p] is None else px > emas[p]) for p in EMA_PERIODS}
    slopes = {}
    for p in EMA_PERIODS:
        s = row.get(f"ema_{p}_slope_1")
        slopes[p] = None if s is None or (isinstance(s, float) and np.isnan(s)) else float(s)

    present = [emas[p] for p in EMA_PERIODS if emas[p] is not None]
    order = "unknown"
    regime = "mixed"
    if all(emas[p] is not None for p in (9, 20, 59)):
        e9, e20, e59 = emas[9], emas[20], emas[59]
        if e9 > e20 > e59:
            order = "bullish_9_20_59"
            regime = "bullish"
        elif e9 < e20 < e59:
            order = "bearish_9_20_59"
            regime = "bearish"
        else:
            order = "mixed_9_20_59"
            regime = "mixed"
        if emas[200] is not None:
            if regime == "bullish" and e59 > emas[200]:
                order = "bullish_9_20_59_200"
            elif regime == "bearish" and e59 < emas[200]:
                order = "bearish_9_20_59_200"

    band_vals = [emas[p] for p in (9, 20, 59) if emas[p] is not None]
    compression = None
    expansion = None
    if len(band_vals) >= 2 and px:
        width = max(band_vals) - min(band_vals)
        compression = width / px
        expansion = width / px

    pool_mid = (pool_lower + pool_upper) / 2.0
    pool_vs_200 = None
    if emas[200] is not None:
        if pool_upper < emas[200]:
            pool_vs_200 = "below"
        elif pool_lower > emas[200]:
            pool_vs_200 = "above"
        else:
            pool_vs_200 = "straddles"

    between_20_59 = False
    if emas[20] is not None and emas[59] is not None:
        lo, hi = sorted((emas[20], emas[59]))
        between_20_59 = lo <= pool_mid <= hi

    touch_ema_band = False
    hi = float(row["high"])
    lo = float(row["low"])
    for p in (9, 20, 59, 200):
        if emas[p] is None:
            continue
        if lo <= emas[p] <= hi:
            touch_ema_band = True
            break

    dist_price = {p: (None if emas[p] is None else (px - emas[p]) / px) for p in EMA_PERIODS}
    dist_pool = {
        p: (None if emas[p] is None else (pool_mid - emas[p]) / emas[p]) for p in EMA_PERIODS
    }

    next_band = None
    if emas[20] is not None and emas[59] is not None:
        if px > max(emas[20], emas[59]):
            next_band = "toward_emas_from_above"
        elif px < min(emas[20], emas[59]):
            next_band = "toward_emas_from_below"
        else:
            next_band = "inside_20_59"

    return {
        "label": label,
        "bar_open_time": str(pd.Timestamp(row["open_time"])),
        "close": px,
        "ema9": emas[9],
        "ema20": emas[20],
        "ema59": emas[59],
        "ema200": emas[200],
        "price_above_ema9": above[9],
        "price_above_ema20": above[20],
        "price_above_ema59": above[59],
        "price_above_ema200": above[200],
        "ema_order": order,
        "ema_regime": regime,
        "ema9_slope": slopes[9],
        "ema20_slope": slopes[20],
        "ema59_slope": slopes[59],
        "ema200_slope": slopes[200],
        "dist_price_ema9": dist_price[9],
        "dist_price_ema20": dist_price[20],
        "dist_price_ema59": dist_price[59],
        "dist_price_ema200": dist_price[200],
        "dist_pool_ema9": dist_pool[9],
        "dist_pool_ema20": dist_pool[20],
        "dist_pool_ema59": dist_pool[59],
        "dist_pool_ema200": dist_pool[200],
        "ema_compression": compression,
        "ema_expansion": expansion,
        "next_ema_band": next_band,
        "pool_vs_ema200": pool_vs_200,
        "pool_between_ema20_59": between_20_59,
        "touch_ema_with_bar": touch_ema_band,
    }

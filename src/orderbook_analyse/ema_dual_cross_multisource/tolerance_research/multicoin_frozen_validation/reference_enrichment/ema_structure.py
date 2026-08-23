"""EMA structure features on completed 5m bars; long/short mirrored."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from .....cluster_sweep_research.ema_features import attach_emas
from .causality import as_utc, direction_sign
from .feature_value import FeatureValue, missing, ok
from .price_atr import atr_series_for_closed

SRC = "candles_5m_completed"


def _safe_div(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    if b == 0 or (isinstance(b, float) and np.isnan(b)):
        return None
    return float(a) / float(b)


def compute_ema_structure_features(
    candles_5m: pd.DataFrame,
    decision_at: datetime | str,
    direction: str,
) -> dict[str, FeatureValue]:
    dec = as_utc(decision_at)
    names = [
        "ema9",
        "ema20",
        "ema59",
        "ema9_slope_1bar_pct",
        "ema20_slope_1bar_pct",
        "ema59_slope_1bar_pct",
        "ema9_slope_atr",
        "ema20_slope_atr",
        "ema59_slope_atr",
        "ema9_20_distance_pct",
        "ema9_20_distance_atr",
        "ema9_59_distance_pct",
        "ema20_59_distance_pct",
        "ema_band_width_pct",
        "ema_band_width_atr",
        "ema_stack_direction",
        "ema_stack_aligned_with_trade",
        "ema59_slope_aligned_with_trade",
        "fast_ema_cohesion",
    ]
    closed, atr = atr_series_for_closed(candles_5m, dec)
    if closed.empty or len(closed) < 59:
        return {n: missing(n, reason="INSUFFICIENT_EMA_WARMUP", status="INSUFFICIENT", source=SRC, asof=dec) for n in names}

    df = attach_emas(closed)
    if len(df) < 2:
        return {n: missing(n, reason="INSUFFICIENT_BARS", status="INSUFFICIENT", source=SRC, asof=dec) for n in names}

    row = df.iloc[-1]
    prev = df.iloc[-2]
    w_end = as_utc(row["open_time"]) + pd.Timedelta(minutes=5)
    close = float(row["close"])
    dsgn = direction_sign(direction)

    def _f(name: str, col: str) -> float | None:
        v = row.get(col)
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return None
        return float(v)

    e9, e20, e59 = _f("ema9", "ema_9"), _f("ema20", "ema_20"), _f("ema59", "ema_59")
    if None in (e9, e20, e59):
        return {n: missing(n, reason="EMA_NAN", status="INSUFFICIENT", source=SRC, asof=dec) for n in names}

    feats: dict[str, FeatureValue] = {}
    feats["ema9"] = ok("ema9", e9, asof=dec, window_start=None, window_end=w_end, source=SRC)
    feats["ema20"] = ok("ema20", e20, asof=dec, window_start=None, window_end=w_end, source=SRC)
    feats["ema59"] = ok("ema59", e59, asof=dec, window_start=None, window_end=w_end, source=SRC)

    for period, key in ((9, "ema9"), (20, "ema20"), (59, "ema59")):
        col = f"ema_{period}"
        cur, prv = float(row[col]), float(prev[col]) if pd.notna(prev[col]) else None
        slope = (cur - prv) if prv is not None else None
        slope_pct = _safe_div(slope * 100.0, close) if slope is not None else None
        slope_atr = _safe_div(slope, atr) if slope is not None else None
        n_pct = f"{key}_slope_1bar_pct"
        n_atr = f"{key}_slope_atr"
        feats[n_pct] = (
            ok(n_pct, slope_pct, asof=dec, window_start=prev["open_time"], window_end=w_end, source=SRC)
            if slope_pct is not None
            else missing(n_pct, reason="SLOPE_INVALID", status="MISSING", source=SRC, asof=dec)
        )
        feats[n_atr] = (
            ok(n_atr, slope_atr, asof=dec, window_start=prev["open_time"], window_end=w_end, source=SRC)
            if slope_atr is not None
            else missing(n_atr, reason="ATR_OR_SLOPE_INVALID", status="MISSING", source=SRC, asof=dec)
        )

    def dist_pct(a: float, b: float) -> float | None:
        return _safe_div((a - b) * 100.0, close)

    def dist_atr(a: float, b: float) -> float | None:
        return _safe_div(a - b, atr)

    # Signed distances (raw); alignment features apply direction separately
    pairs = [
        ("ema9_20_distance_pct", e9, e20, False),
        ("ema9_20_distance_atr", e9, e20, True),
        ("ema9_59_distance_pct", e9, e59, False),
        ("ema20_59_distance_pct", e20, e59, False),
    ]
    for name, a, b, use_atr in pairs:
        v = dist_atr(a, b) if use_atr else dist_pct(a, b)
        feats[name] = (
            ok(name, v, asof=dec, window_start=None, window_end=w_end, source=SRC)
            if v is not None
            else missing(name, reason="DIST_INVALID", status="MISSING", source=SRC, asof=dec)
        )

    band = max(e9, e20, e59) - min(e9, e20, e59)
    band_pct = _safe_div(band * 100.0, close)
    band_atr = _safe_div(band, atr)
    feats["ema_band_width_pct"] = (
        ok("ema_band_width_pct", band_pct, asof=dec, window_start=None, window_end=w_end, source=SRC)
        if band_pct is not None
        else missing("ema_band_width_pct", reason="INVALID", status="MISSING", source=SRC, asof=dec)
    )
    feats["ema_band_width_atr"] = (
        ok("ema_band_width_atr", band_atr, asof=dec, window_start=None, window_end=w_end, source=SRC)
        if band_atr is not None
        else missing("ema_band_width_atr", reason="ATR_INVALID", status="MISSING", source=SRC, asof=dec)
    )

    # Stack: +1 bull (9&20>59), -1 bear (9&20<59), 0 otherwise — descriptive, no threshold
    if e9 > e59 and e20 > e59:
        stack = 1
    elif e9 < e59 and e20 < e59:
        stack = -1
    else:
        stack = 0
    feats["ema_stack_direction"] = ok("ema_stack_direction", stack, asof=dec, window_start=None, window_end=w_end, source=SRC)
    # Aligned: +1 if stack matches trade direction, -1 if opposite, 0 if flat stack
    if stack == 0:
        aligned = 0
    else:
        aligned = 1 if stack == dsgn else -1
    feats["ema_stack_aligned_with_trade"] = ok(
        "ema_stack_aligned_with_trade", aligned, asof=dec, window_start=None, window_end=w_end, source=SRC
    )

    slope59 = float(row["ema_59"]) - float(prev["ema_59"]) if pd.notna(prev["ema_59"]) else None
    if slope59 is None:
        feats["ema59_slope_aligned_with_trade"] = missing(
            "ema59_slope_aligned_with_trade", reason="SLOPE_INVALID", status="MISSING", source=SRC, asof=dec
        )
    else:
        # Positive when slope sign matches trade direction
        feats["ema59_slope_aligned_with_trade"] = ok(
            "ema59_slope_aligned_with_trade",
            float(np.sign(slope59) * dsgn),
            asof=dec,
            window_start=prev["open_time"],
            window_end=w_end,
            source=SRC,
        )

    # Cohesion: how tight 9 vs 20 relative to ATR (smaller = more cohesive); always >= 0
    cohesion = _safe_div(abs(e9 - e20), atr)
    feats["fast_ema_cohesion"] = (
        ok("fast_ema_cohesion", cohesion, asof=dec, window_start=None, window_end=w_end, source=SRC)
        if cohesion is not None
        else missing("fast_ema_cohesion", reason="ATR_INVALID", status="MISSING", source=SRC, asof=dec)
    )
    return feats

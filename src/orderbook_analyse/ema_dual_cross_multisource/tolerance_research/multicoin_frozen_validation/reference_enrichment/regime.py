"""Descriptive trend/range regime features (no optimized thresholds)."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from . import constants as C
from .causality import as_utc
from .feature_value import FeatureValue, missing, ok
from .price_atr import atr_series_for_closed, atr_wilder_like, true_range

SRC = "candles_5m_completed"


def _safe_div(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    if b == 0 or (isinstance(b, float) and np.isnan(b)):
        return None
    return float(a) / float(b)


def compute_regime_features(
    candles_5m: pd.DataFrame,
    decision_at: datetime | str,
) -> dict[str, FeatureValue]:
    dec = as_utc(decision_at)
    names = [
        "return_1h_pct",
        "return_4h_pct",
        "absolute_return_1h_pct",
        "absolute_return_4h_pct",
        "efficiency_ratio_1h",
        "efficiency_ratio_4h",
        "close_position_in_1h_range",
        "close_position_in_4h_range",
        "atr_short_long_ratio",
        "distance_from_1h_high_pct",
        "distance_from_1h_low_pct",
        "distance_from_4h_high_pct",
        "distance_from_4h_low_pct",
    ]
    closed, atr14 = atr_series_for_closed(candles_5m, dec)
    if closed.empty:
        return {n: missing(n, reason="NO_BARS", status="INSUFFICIENT", source=SRC, asof=dec) for n in names}

    closed = closed.copy()
    closed["tr"] = true_range(closed["high"], closed["low"], closed["close"])
    closed["atr_long"] = atr_wilder_like(closed["tr"], C.ATR_LONG_PERIOD)
    sig = closed.iloc[-1]
    close = float(sig["close"])
    w_end = as_utc(sig["open_time"]) + pd.Timedelta(minutes=5)
    feats: dict[str, FeatureValue] = {}

    def _window(n_bars: int):
        if len(closed) < n_bars:
            return None
        return closed.iloc[-n_bars:]

    def _return_pct(n_bars: int, name: str) -> FeatureValue:
        sl = _window(n_bars)
        if sl is None:
            return missing(name, reason="INSUFFICIENT_BARS", status="INSUFFICIENT", source=SRC, asof=dec)
        c0 = float(sl.iloc[0]["close"])
        v = _safe_div((close - c0) * 100.0, c0)
        if v is None:
            return missing(name, reason="INVALID", status="MISSING", source=SRC, asof=dec)
        return ok(name, v, asof=dec, window_start=sl.iloc[0]["open_time"], window_end=w_end, source=SRC)

    for n_bars, label in ((12, "1h"), (48, "4h")):
        r = _return_pct(n_bars, f"return_{label}_pct")
        feats[f"return_{label}_pct"] = r
        if r.value is None:
            feats[f"absolute_return_{label}_pct"] = missing(
                f"absolute_return_{label}_pct", reason=r.missing_reason or "MISSING", status=r.coverage_status, source=SRC, asof=dec
            )
        else:
            feats[f"absolute_return_{label}_pct"] = ok(
                f"absolute_return_{label}_pct", abs(float(r.value)), asof=dec, window_start=r.window_start, window_end=w_end, source=SRC
            )

        sl = _window(n_bars)
        if sl is None:
            feats[f"efficiency_ratio_{label}"] = missing(
                f"efficiency_ratio_{label}", reason="INSUFFICIENT_BARS", status="INSUFFICIENT", source=SRC, asof=dec
            )
            feats[f"close_position_in_{label}_range"] = missing(
                f"close_position_in_{label}_range", reason="INSUFFICIENT_BARS", status="INSUFFICIENT", source=SRC, asof=dec
            )
            feats[f"distance_from_{label}_high_pct"] = missing(
                f"distance_from_{label}_high_pct", reason="INSUFFICIENT_BARS", status="INSUFFICIENT", source=SRC, asof=dec
            )
            feats[f"distance_from_{label}_low_pct"] = missing(
                f"distance_from_{label}_low_pct", reason="INSUFFICIENT_BARS", status="INSUFFICIENT", source=SRC, asof=dec
            )
            continue

        # Efficiency: |net move| / sum(|bar moves|)
        closes = sl["close"].astype(float)
        net = abs(float(closes.iloc[-1] - closes.iloc[0]))
        path = float(closes.diff().abs().sum())
        eff = _safe_div(net, path) if path > 0 else None
        feats[f"efficiency_ratio_{label}"] = (
            ok(f"efficiency_ratio_{label}", eff, asof=dec, window_start=sl.iloc[0]["open_time"], window_end=w_end, source=SRC)
            if eff is not None
            else missing(f"efficiency_ratio_{label}", reason="ZERO_PATH", status="MISSING", source=SRC, asof=dec)
        )

        hi, lo = float(sl["high"].max()), float(sl["low"].min())
        rng = hi - lo
        pos = _safe_div(close - lo, rng) if rng > 0 else None
        feats[f"close_position_in_{label}_range"] = (
            ok(f"close_position_in_{label}_range", pos, asof=dec, window_start=sl.iloc[0]["open_time"], window_end=w_end, source=SRC)
            if pos is not None
            else missing(f"close_position_in_{label}_range", reason="ZERO_RANGE", status="MISSING", source=SRC, asof=dec)
        )
        d_hi = _safe_div((hi - close) * 100.0, close)
        d_lo = _safe_div((close - lo) * 100.0, close)
        feats[f"distance_from_{label}_high_pct"] = (
            ok(f"distance_from_{label}_high_pct", d_hi, asof=dec, window_start=sl.iloc[0]["open_time"], window_end=w_end, source=SRC)
            if d_hi is not None
            else missing(f"distance_from_{label}_high_pct", reason="INVALID", status="MISSING", source=SRC, asof=dec)
        )
        feats[f"distance_from_{label}_low_pct"] = (
            ok(f"distance_from_{label}_low_pct", d_lo, asof=dec, window_start=sl.iloc[0]["open_time"], window_end=w_end, source=SRC)
            if d_lo is not None
            else missing(f"distance_from_{label}_low_pct", reason="INVALID", status="MISSING", source=SRC, asof=dec)
        )

    atr_long = float(sig["atr_long"]) if pd.notna(sig.get("atr_long")) else None
    if atr_long is not None and atr_long <= 0:
        atr_long = None
    ratio = _safe_div(atr14, atr_long)
    feats["atr_short_long_ratio"] = (
        ok("atr_short_long_ratio", ratio, asof=dec, window_start=None, window_end=w_end, source=SRC)
        if ratio is not None
        else missing("atr_short_long_ratio", reason="ATR_BASELINE_INVALID", status="INSUFFICIENT", source=SRC, asof=dec)
    )
    return feats

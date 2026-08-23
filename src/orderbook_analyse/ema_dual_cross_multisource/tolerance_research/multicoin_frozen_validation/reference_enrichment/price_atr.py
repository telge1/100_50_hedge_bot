"""Price / ATR features from completed 5m candles (causal)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from . import constants as C
from .causality import as_utc, completed_bars
from .feature_value import FeatureValue, missing, ok

SRC = "candles_5m_completed"


def _safe_div(num: float | None, den: float | None) -> float | None:
    if num is None or den is None:
        return None
    if den == 0 or (isinstance(den, float) and (np.isnan(den) or den == 0.0)):
        return None
    return float(num) / float(den)


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_c = close.shift(1)
    return pd.concat([(high - low).abs(), (high - prev_c).abs(), (low - prev_c).abs()], axis=1).max(axis=1)


def atr_wilder_like(tr: pd.Series, period: int) -> pd.Series:
    """Simple rolling mean ATR (matches attach_atr in ema_candidate)."""
    return tr.rolling(period, min_periods=period).mean()


def realized_vol_log_returns(closes: pd.Series) -> float | None:
    if closes is None or len(closes) < 2:
        return None
    c = closes.astype(float)
    if (c <= 0).any():
        return None
    lr = np.log(c / c.shift(1)).dropna()
    if lr.empty:
        return None
    return float(lr.std(ddof=0) * np.sqrt(len(lr)))


def compute_price_atr_features(
    candles_5m: pd.DataFrame,
    decision_at: datetime | str,
) -> dict[str, FeatureValue]:
    dec = as_utc(decision_at)
    closed = completed_bars(candles_5m, dec, tf_minutes=5)
    names = [
        "close_at_decision",
        "atr14_abs",
        "atr14_pct",
        "signal_bar_range_pct",
        "signal_bar_body_pct",
        "signal_bar_range_atr",
        "rolling_range_1h_pct",
        "rolling_range_4h_pct",
        "realized_volatility_1h",
        "realized_volatility_4h",
        "tp_pct",
        "sl_pct",
        "tp_atr_ratio",
        "sl_atr_ratio",
        "reward_risk_gross",
        "net_tp_pct",
        "net_sl_pct",
    ]
    if closed.empty or len(closed) < 2:
        return {n: missing(n, reason="INSUFFICIENT_COMPLETED_BARS", status="INSUFFICIENT", source=SRC, asof=dec) for n in names}

    out = closed.copy()
    for col in ("open", "high", "low", "close"):
        out[col] = out[col].astype(float)
    out["tr"] = true_range(out["high"], out["low"], out["close"])
    out["atr14"] = atr_wilder_like(out["tr"], C.ATR_PERIOD)

    sig = out.iloc[-1]
    w_end = as_utc(sig["open_time"]) + pd.Timedelta(minutes=5)
    close = float(sig["close"])
    atr_abs = float(sig["atr14"]) if pd.notna(sig["atr14"]) else None
    if atr_abs is not None and (atr_abs <= 0 or np.isnan(atr_abs)):
        atr_abs = None
    atr_pct = _safe_div(atr_abs * 100.0, close) if atr_abs is not None else None

    range_abs = float(sig["high"] - sig["low"])
    body_abs = abs(float(sig["close"] - sig["open"]))
    range_pct = _safe_div(range_abs * 100.0, close)
    body_pct = _safe_div(body_abs * 100.0, close)
    range_atr = _safe_div(range_abs, atr_abs)

    def _rolling_range_pct(n_bars: int) -> FeatureValue:
        name = f"rolling_range_{n_bars // 12}h_pct" if n_bars in (12, 48) else f"rolling_range_{n_bars}bars_pct"
        # 1h=12*5m, 4h=48*5m
        if len(out) < n_bars:
            return missing(name, reason="INSUFFICIENT_BARS", status="INSUFFICIENT", source=SRC, asof=dec)
        sl = out.iloc[-n_bars:]
        hi, lo = float(sl["high"].max()), float(sl["low"].min())
        v = _safe_div((hi - lo) * 100.0, close)
        if v is None:
            return missing(name, reason="INVALID_RANGE", status="MISSING", source=SRC, asof=dec)
        return ok(name, v, asof=dec, window_start=sl.iloc[0]["open_time"], window_end=w_end, source=SRC)

    def _rv(n_bars: int, name: str) -> FeatureValue:
        if len(out) < n_bars:
            return missing(name, reason="INSUFFICIENT_BARS", status="INSUFFICIENT", source=SRC, asof=dec)
        sl = out.iloc[-n_bars:]
        v = realized_vol_log_returns(sl["close"])
        if v is None:
            return missing(name, reason="INVALID_VOL", status="MISSING", source=SRC, asof=dec)
        return ok(name, v, asof=dec, window_start=sl.iloc[0]["open_time"], window_end=w_end, source=SRC)

    feats: dict[str, FeatureValue] = {}
    feats["close_at_decision"] = ok("close_at_decision", close, asof=dec, window_start=sig["open_time"], window_end=w_end, source=SRC)
    if atr_abs is None:
        feats["atr14_abs"] = missing("atr14_abs", reason="ATR_UNAVAILABLE", status="INSUFFICIENT", source=SRC, asof=dec)
        feats["atr14_pct"] = missing("atr14_pct", reason="ATR_UNAVAILABLE", status="INSUFFICIENT", source=SRC, asof=dec)
    else:
        feats["atr14_abs"] = ok("atr14_abs", atr_abs, asof=dec, window_start=out.iloc[-C.ATR_PERIOD]["open_time"], window_end=w_end, source=SRC)
        feats["atr14_pct"] = (
            ok("atr14_pct", atr_pct, asof=dec, window_start=out.iloc[-C.ATR_PERIOD]["open_time"], window_end=w_end, source=SRC)
            if atr_pct is not None
            else missing("atr14_pct", reason="DIV_BY_ZERO_CLOSE", status="MISSING", source=SRC, asof=dec)
        )

    feats["signal_bar_range_pct"] = (
        ok("signal_bar_range_pct", range_pct, asof=dec, window_start=sig["open_time"], window_end=w_end, source=SRC)
        if range_pct is not None
        else missing("signal_bar_range_pct", reason="INVALID", status="MISSING", source=SRC, asof=dec)
    )
    feats["signal_bar_body_pct"] = (
        ok("signal_bar_body_pct", body_pct, asof=dec, window_start=sig["open_time"], window_end=w_end, source=SRC)
        if body_pct is not None
        else missing("signal_bar_body_pct", reason="INVALID", status="MISSING", source=SRC, asof=dec)
    )
    feats["signal_bar_range_atr"] = (
        ok("signal_bar_range_atr", range_atr, asof=dec, window_start=sig["open_time"], window_end=w_end, source=SRC)
        if range_atr is not None
        else missing("signal_bar_range_atr", reason="ATR_OR_RANGE_INVALID", status="MISSING", source=SRC, asof=dec)
    )
    feats["rolling_range_1h_pct"] = _rolling_range_pct(12)
    feats["rolling_range_4h_pct"] = _rolling_range_pct(48)
    feats["realized_volatility_1h"] = _rv(12, "realized_volatility_1h")
    feats["realized_volatility_4h"] = _rv(48, "realized_volatility_4h")

    # Fixed strategy relations (not market-derived except ATR ratios)
    feats["tp_pct"] = ok("tp_pct", C.REF_TP_PCT, asof=dec, window_start=None, window_end=None, source="reference_strategy")
    feats["sl_pct"] = ok("sl_pct", C.REF_SL_PCT, asof=dec, window_start=None, window_end=None, source="reference_strategy")
    feats["reward_risk_gross"] = ok(
        "reward_risk_gross", C.REF_TP_PCT / C.REF_SL_PCT, asof=dec, window_start=None, window_end=None, source="reference_strategy"
    )
    feats["net_tp_pct"] = ok("net_tp_pct", C.REF_TP_PCT - C.REF_COST_PCT, asof=dec, window_start=None, window_end=None, source="reference_strategy")
    feats["net_sl_pct"] = ok("net_sl_pct", C.REF_SL_PCT + C.REF_COST_PCT, asof=dec, window_start=None, window_end=None, source="reference_strategy")

    tp_atr = _safe_div(C.REF_TP_PCT, atr_pct)
    sl_atr = _safe_div(C.REF_SL_PCT, atr_pct)
    feats["tp_atr_ratio"] = (
        ok("tp_atr_ratio", tp_atr, asof=dec, window_start=None, window_end=w_end, source=SRC)
        if tp_atr is not None
        else missing("tp_atr_ratio", reason="ATR_PCT_INVALID", status="MISSING", source=SRC, asof=dec)
    )
    feats["sl_atr_ratio"] = (
        ok("sl_atr_ratio", sl_atr, asof=dec, window_start=None, window_end=w_end, source=SRC)
        if sl_atr is not None
        else missing("sl_atr_ratio", reason="ATR_PCT_INVALID", status="MISSING", source=SRC, asof=dec)
    )
    return feats


def atr_series_for_closed(candles_5m: pd.DataFrame, decision_at: datetime | str, period: int = C.ATR_PERIOD) -> tuple[pd.DataFrame, float | None]:
    """Helper for other feature modules: closed bars + atr14 at signal."""
    closed = completed_bars(candles_5m, decision_at, tf_minutes=5)
    if closed.empty:
        return closed, None
    out = closed.copy()
    for col in ("open", "high", "low", "close"):
        out[col] = out[col].astype(float)
    out["tr"] = true_range(out["high"], out["low"], out["close"])
    out["atr14"] = atr_wilder_like(out["tr"], period)
    out[f"atr{period}"] = out["atr14"]
    if period != C.ATR_LONG_PERIOD:
        out[f"atr{C.ATR_LONG_PERIOD}"] = atr_wilder_like(out["tr"], C.ATR_LONG_PERIOD)
    atr = float(out.iloc[-1]["atr14"]) if pd.notna(out.iloc[-1]["atr14"]) else None
    if atr is not None and atr <= 0:
        atr = None
    return out, atr

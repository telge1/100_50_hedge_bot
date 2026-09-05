"""Causal 5m EMA/ATR/swing indicators (closed candles only)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows import (
    ATR_PERIOD,
    MISSING,
)


def _to_utc_ts(dt: datetime) -> pd.Timestamp:
    if isinstance(dt, pd.Timestamp):
        return dt.tz_convert("UTC") if dt.tzinfo else dt.tz_localize("UTC")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return pd.Timestamp(dt).tz_convert("UTC")


def aggregate_5m(candles_1m: pd.DataFrame) -> pd.DataFrame:
    """Build closed 5m bars from 1m OHLCV-like frame (open_time, open, high, low, close)."""
    if candles_1m.empty:
        return candles_1m.copy()
    df = candles_1m.copy()
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    df = df.set_index("open_time").sort_index()
    ohlc = df.resample("5min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    )
    ohlc = ohlc.dropna(subset=["open", "close"])
    ohlc["bar_end"] = ohlc.index + pd.Timedelta(minutes=5)
    return ohlc.reset_index()


def ema_series(close: pd.Series, span: int) -> pd.Series:
    return close.astype(float).ewm(span=span, adjust=False).mean()


def atr_series(bars: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    close = bars["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def prepare_5m_indicators(candles_1m: pd.DataFrame) -> pd.DataFrame:
    bars = aggregate_5m(candles_1m)
    if bars.empty:
        return bars
    bars["ema9"] = ema_series(bars["close"], 9)
    bars["ema20"] = ema_series(bars["close"], 20)
    bars["ema59"] = ema_series(bars["close"], 59)
    bars["atr"] = atr_series(bars, ATR_PERIOD)
    # Warm-up completeness: require index position >= max span-1
    bars["warmup_ok"] = bars.index >= max(58, ATR_PERIOD)
    return bars


def last_closed_bar_at(bars: pd.DataFrame, asof: datetime) -> pd.Series | None:
    """Last fully closed 5m bar with bar_end <= asof (no open candle)."""
    if bars.empty:
        return None
    asof_ts = _to_utc_ts(asof)
    closed = bars[bars["bar_end"] <= asof_ts]
    if closed.empty:
        return None
    return closed.iloc[-1]


def slope_over(bars: pd.DataFrame, col: str, asof: datetime, n: int) -> float | None:
    """Slope of col over last n closed bars ending at asof (inclusive last closed)."""
    asof_ts = _to_utc_ts(asof)
    closed = bars[bars["bar_end"] <= asof_ts]
    if len(closed) < n + 1:
        return None
    y = closed[col].astype(float).iloc[-(n + 1) :].to_numpy()
    x = np.arange(len(y), dtype=float)
    # linear regression slope per bar
    return float(np.polyfit(x, y, 1)[0])


def find_swings(
    bars: pd.DataFrame, asof: datetime, *, lookback: int = 36, wing: int = 2
) -> dict[str, Any]:
    """Confirmed swing highs/lows using only closed bars before asof."""
    asof_ts = _to_utc_ts(asof)
    closed = bars[bars["bar_end"] <= asof_ts].tail(lookback + 2 * wing)
    if len(closed) < 2 * wing + 3:
        return {
            "last_swing_high": MISSING,
            "last_swing_low": MISSING,
            "structure": "UNDETERMINED",
        }
    highs = closed["high"].astype(float).to_numpy()
    lows = closed["low"].astype(float).to_numpy()
    times = closed["bar_end"].tolist()
    sh: list[tuple[Any, float]] = []
    sl: list[tuple[Any, float]] = []
    for i in range(wing, len(closed) - wing):
        if highs[i] == max(highs[i - wing : i + wing + 1]):
            sh.append((times[i], float(highs[i])))
        if lows[i] == min(lows[i - wing : i + wing + 1]):
            sl.append((times[i], float(lows[i])))
    structure = "UNDETERMINED"
    if len(sh) >= 2 and len(sl) >= 2:
        hh = sh[-1][1] > sh[-2][1]
        hl = sl[-1][1] > sl[-2][1]
        lh = sh[-1][1] < sh[-2][1]
        ll = sl[-1][1] < sl[-2][1]
        if hh and hl:
            structure = "HH_HL"
        elif lh and ll:
            structure = "LH_LL"
        elif hh and ll:
            structure = "MIXED_HH_LL"
        elif lh and hl:
            structure = "MIXED_LH_HL"
        else:
            structure = "MIXED"
    return {
        "last_swing_high": sh[-1][1] if sh else MISSING,
        "last_swing_high_at": str(sh[-1][0]) if sh else MISSING,
        "last_swing_low": sl[-1][1] if sl else MISSING,
        "last_swing_low_at": str(sl[-1][0]) if sl else MISSING,
        "structure": structure,
    }


@dataclass
class TrendSnapshot:
    asof_utc: str
    classification: str
    confidence: float
    reasons: str
    ema9: float | None
    ema20: float | None
    ema59: float | None
    atr: float | None
    close: float | None
    last_bar_end: str | None
    ema20_slope_3: float | None
    ema20_slope_6: float | None
    ema59_slope_3: float | None
    ema59_slope_6: float | None
    ret_15m: float | None
    ret_30m: float | None
    ret_60m: float | None
    structure: str
    warmup_ok: bool
    score_components: str


def _pre_event_return(bars: pd.DataFrame, asof: datetime, minutes: int) -> float | None:
    row = last_closed_bar_at(bars, asof)
    if row is None:
        return None
    asof_ts = _to_utc_ts(asof)
    target_end = asof_ts - pd.Timedelta(minutes=minutes)
    # find closed bar with bar_end <= target_end closest
    prior = bars[bars["bar_end"] <= target_end]
    if prior.empty:
        return None
    p0 = float(prior.iloc[-1]["close"])
    p1 = float(row["close"])
    if p0 <= 0:
        return None
    return (p1 / p0 - 1.0) * 100.0


def classify_trend(bars: pd.DataFrame, asof: datetime) -> TrendSnapshot:
    """Causal trend at asof using only closed 5m bars."""
    asof_s = _to_utc_ts(asof).isoformat().replace("+00:00", "Z")
    row = last_closed_bar_at(bars, asof)
    empty = TrendSnapshot(
        asof_utc=asof_s,
        classification="UNDETERMINED",
        confidence=0.0,
        reasons="no_closed_5m_bar",
        ema9=None,
        ema20=None,
        ema59=None,
        atr=None,
        close=None,
        last_bar_end=None,
        ema20_slope_3=None,
        ema20_slope_6=None,
        ema59_slope_3=None,
        ema59_slope_6=None,
        ret_15m=None,
        ret_30m=None,
        ret_60m=None,
        structure="UNDETERMINED",
        warmup_ok=False,
        score_components="",
    )
    if row is None:
        return empty
    if not bool(row.get("warmup_ok", False)):
        empty.reasons = "incomplete_ema59_warmup"
        empty.last_bar_end = str(row["bar_end"])
        return empty

    ema9 = float(row["ema9"])
    ema20 = float(row["ema20"])
    ema59 = float(row["ema59"])
    atr = float(row["atr"])
    close = float(row["close"])
    s20_3 = slope_over(bars, "ema20", asof, 3)
    s20_6 = slope_over(bars, "ema20", asof, 6)
    s59_3 = slope_over(bars, "ema59", asof, 3)
    s59_6 = slope_over(bars, "ema59", asof, 6)
    swings = find_swings(bars, asof)
    structure = str(swings["structure"])
    ret15 = _pre_event_return(bars, asof, 15)
    ret30 = _pre_event_return(bars, asof, 30)
    ret60 = _pre_event_return(bars, asof, 60)

    # Score components (documented weights, sum to 1.0)
    # stack 0.30 | slopes 0.25 | price vs ema20 0.20 | structure 0.15 | returns 0.10
    components: dict[str, float] = {}
    reasons: list[str] = []

    stacked_bear = ema9 < ema20 < ema59
    stacked_bull = ema9 > ema20 > ema59
    if stacked_bear:
        components["stack"] = 0.30
        reasons.append("EMA9<EMA20<EMA59")
    elif stacked_bull:
        components["stack"] = -0.30
        reasons.append("EMA9>EMA20>EMA59")
    else:
        components["stack"] = 0.0
        reasons.append("EMA_stack_mixed")

    slope_bear = (s20_3 is not None and s20_3 < 0) and (s59_3 is not None and s59_3 < 0)
    slope_bull = (s20_3 is not None and s20_3 > 0) and (s59_3 is not None and s59_3 > 0)
    if slope_bear:
        components["slopes"] = 0.25
        reasons.append("EMA20+EMA59_slopes_down")
    elif slope_bull:
        components["slopes"] = -0.25
        reasons.append("EMA20+EMA59_slopes_up")
    else:
        components["slopes"] = 0.0
        reasons.append("EMA_slopes_mixed")

    if close < ema20:
        components["price_ema20"] = 0.20
        reasons.append("close_below_EMA20")
    elif close > ema20:
        components["price_ema20"] = -0.20
        reasons.append("close_above_EMA20")
    else:
        components["price_ema20"] = 0.0

    if structure == "LH_LL":
        components["structure"] = 0.15
        reasons.append("LH_LL")
    elif structure == "HH_HL":
        components["structure"] = -0.15
        reasons.append("HH_HL")
    else:
        components["structure"] = 0.0
        reasons.append(f"structure={structure}")

    if ret30 is not None and ret30 < -0.05:
        components["returns"] = 0.10
        reasons.append(f"ret30={ret30:.3f}%")
    elif ret30 is not None and ret30 > 0.05:
        components["returns"] = -0.10
        reasons.append(f"ret30={ret30:.3f}%")
    else:
        components["returns"] = 0.0
        reasons.append(f"ret30={ret30}")

    score = sum(components.values())  # +bearish, -bullish; range [-1,1]
    abs_conf = abs(score)

    # Transition: price/EMA9 crossed EMA20 but stack not fully bull/bear confirmed
    transition = False
    if close > ema20 and ema20 < ema59 and not stacked_bull:
        transition = True
        reasons.append("TRANSITION_price_above_EMA20_but_EMA20<EMA59")
    if close < ema20 and ema20 > ema59 and not stacked_bear:
        transition = True
        reasons.append("TRANSITION_price_below_EMA20_but_EMA20>EMA59")

    # Range: flat slopes and close near EMA20
    near_ema20 = atr > 0 and abs(close - ema20) / atr < 0.35
    flat = (
        s20_3 is not None
        and s59_3 is not None
        and abs(s20_3) < atr * 0.02
        and abs(s59_3) < atr * 0.01
    )
    if transition:
        classification = "TRANSITION"
    elif stacked_bear and slope_bear and close < ema20:
        classification = "BEARISH"
    elif stacked_bull and slope_bull and close > ema20:
        classification = "BULLISH"
    elif flat and near_ema20 and structure not in ("LH_LL", "HH_HL"):
        classification = "RANGE"
    elif score > 0.35:
        classification = "BEARISH"
    elif score < -0.35:
        classification = "BULLISH"
    else:
        classification = "UNDETERMINED"
        reasons.append("score_ambiguous")

    # confidence: absolute score, clipped; transition capped
    confidence = float(min(1.0, abs_conf))
    if classification == "TRANSITION":
        confidence = float(min(confidence, 0.65))
    if classification == "UNDETERMINED":
        confidence = float(min(confidence, 0.40))

    return TrendSnapshot(
        asof_utc=asof_s,
        classification=classification,
        confidence=confidence,
        reasons="|".join(reasons),
        ema9=ema9,
        ema20=ema20,
        ema59=ema59,
        atr=atr,
        close=close,
        last_bar_end=str(row["bar_end"]),
        ema20_slope_3=s20_3,
        ema20_slope_6=s20_6,
        ema59_slope_3=s59_3,
        ema59_slope_6=s59_6,
        ret_15m=ret15,
        ret_30m=ret30,
        ret_60m=ret60,
        structure=structure,
        warmup_ok=True,
        score_components=";".join(f"{k}={v:.2f}" for k, v in components.items()),
    )


def ema_lookup_series(bars: pd.DataFrame) -> pd.DataFrame:
    """Step series: for each closed bar, EMA valid from bar_end forward until next."""
    return bars[
        ["bar_end", "ema9", "ema20", "ema59", "atr", "close", "warmup_ok"]
    ].copy()


def causal_ema_at(bars: pd.DataFrame, asof: datetime) -> dict[str, float | None | bool]:
    row = last_closed_bar_at(bars, asof)
    if row is None:
        return {
            "ema9": None,
            "ema20": None,
            "ema59": None,
            "atr": None,
            "close": None,
            "warmup_ok": False,
        }
    return {
        "ema9": float(row["ema9"]),
        "ema20": float(row["ema20"]),
        "ema59": float(row["ema59"]),
        "atr": float(row["atr"]),
        "close": float(row["close"]),
        "warmup_ok": bool(row.get("warmup_ok", False)),
    }

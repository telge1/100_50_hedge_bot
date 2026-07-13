"""LuxAlgo Smart Money Concepts — structure-only research reference (read-only).

Attribution
-----------
This module reimplements a **subset** of the market-structure logic from:

  Smart Money Concepts [LuxAlgo]
  https://www.luxalgo.com/library/indicator/smart-money-concepts-smc
  (also mirrored in public TradingView / gist distributions of the same script)

Original work © LuxAlgo. Licensed under **Creative Commons Attribution-
NonCommercial-ShareAlike 4.0 International (CC BY-NC-SA 4.0)**:
  https://creativecommons.org/licenses/by-nc-sa/4.0/

This research port is provided for non-commercial analysis only, attributes
LuxAlgo as the source of the structure semantics, and is intended to remain
under the same license terms for derivative research artifacts.

Ported (structure only)
-----------------------
- leg(size) / startOfNewLeg / startOfBearishLeg / startOfBullishLeg
- Internal & swing pivot highs/lows (confirmed ``size`` bars later)
- HH / HL / LH / LL classification at pivot confirmation
- Internal & swing BOS / CHoCH via close cross of active pivot level
- Internal & swing bias

Not ported
----------
Order blocks, FVGs, EQH/EQL, premium/discount, MTF levels, drawing, alerts,
or any trading / policy logic.

Pine ``ta.highest(size)`` / ``ta.lowest(size)`` semantics (v5)
-------------------------------------------------------------
``ta.highest(size)`` ≡ max of ``high[0] .. high[size-1]`` (current bar included,
the candidate ``high[size]`` is **excluded**). Same for lowest/low.

Decision timestamps never backpaint: pivot candle time is stored separately
from confirmation / event decision time (the closed bar that confirms).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
import pandas as pd

BEARISH_LEG = 0
BULLISH_LEG = 1
BEARISH = -1
BULLISH = +1

SwingPointType = Literal["HH", "HL", "LH", "LL", ""]


def _ts(v: object) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(v: object | None) -> str | None:
    if v is None:
        return None
    return _ts(v).isoformat()


def pine_highest(highs: np.ndarray, i: int, size: int) -> float:
    """Pine ``ta.highest(size)`` at bar ``i``: max of high[i-size+1 : i+1]."""
    start = i - size + 1
    if start < 0:
        start = 0
    return float(np.max(highs[start : i + 1]))


def pine_lowest(lows: np.ndarray, i: int, size: int) -> float:
    """Pine ``ta.lowest(size)`` at bar ``i``: min of low[i-size+1 : i+1]."""
    start = i - size + 1
    if start < 0:
        start = 0
    return float(np.min(lows[start : i + 1]))


def new_leg_high(highs: np.ndarray, i: int, size: int) -> bool:
    """``high[size] > ta.highest(size)`` at bar i (requires i >= size)."""
    if i < size:
        return False
    # Compare candidate high[size] vs max of more-recent size bars (exclude candidate)
    # Pine: ta.highest(size) = max(high[0]..high[size-1]) relative to current
    window_max = float(np.max(highs[i - size + 1 : i + 1]))
    return float(highs[i - size]) > window_max


def new_leg_low(lows: np.ndarray, i: int, size: int) -> bool:
    """``low[size] < ta.lowest(size)`` at bar i (requires i >= size)."""
    if i < size:
        return False
    window_min = float(np.min(lows[i - size + 1 : i + 1]))
    return float(lows[i - size]) < window_min


@dataclass
class PivotState:
    current_level: float | None = None
    last_level: float | None = None
    crossed: bool = False
    bar_time: pd.Timestamp | None = None
    bar_index: int | None = None


@dataclass
class TrendState:
    bias: int = 0  # 0 unknown until first BOS/CHoCH; LuxAlgo starts unset — we use 0


@dataclass
class StructureBar:
    """One closed-bar snapshot (decision-time = this bar's close / decision)."""

    timestamp_utc: str
    timeframe: str
    close: float
    internal_leg: int
    swing_leg: int
    internal_pivot_high: float | None
    internal_pivot_low: float | None
    swing_pivot_high: float | None
    swing_pivot_low: float | None
    internal_pivot_high_timestamp: str | None
    internal_pivot_low_timestamp: str | None
    swing_pivot_high_timestamp: str | None
    swing_pivot_low_timestamp: str | None
    internal_bias: int
    swing_bias: int
    internal_bullish_bos: bool
    internal_bearish_bos: bool
    internal_bullish_choch: bool
    internal_bearish_choch: bool
    swing_bullish_bos: bool
    swing_bearish_bos: bool
    swing_bullish_choch: bool
    swing_bearish_choch: bool
    swing_point_type: str
    broken_level: float | None
    event_decision_timestamp: str
    # diagnostic
    pivot_candle_timestamp: str | None = None
    confirmation_timestamp: str | None = None
    internal_new_pivot_high: bool = False
    internal_new_pivot_low: bool = False
    swing_new_pivot_high: bool = False
    swing_new_pivot_low: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp_utc": self.timestamp_utc,
            "timeframe": self.timeframe,
            "close": self.close,
            "internal_leg": self.internal_leg,
            "swing_leg": self.swing_leg,
            "internal_pivot_high": self.internal_pivot_high,
            "internal_pivot_low": self.internal_pivot_low,
            "swing_pivot_high": self.swing_pivot_high,
            "swing_pivot_low": self.swing_pivot_low,
            "internal_pivot_high_timestamp": self.internal_pivot_high_timestamp,
            "internal_pivot_low_timestamp": self.internal_pivot_low_timestamp,
            "swing_pivot_high_timestamp": self.swing_pivot_high_timestamp,
            "swing_pivot_low_timestamp": self.swing_pivot_low_timestamp,
            "internal_bias": self.internal_bias,
            "swing_bias": self.swing_bias,
            "internal_bullish_bos": self.internal_bullish_bos,
            "internal_bearish_bos": self.internal_bearish_bos,
            "internal_bullish_choch": self.internal_bullish_choch,
            "internal_bearish_choch": self.internal_bearish_choch,
            "swing_bullish_bos": self.swing_bullish_bos,
            "swing_bearish_bos": self.swing_bearish_bos,
            "swing_bullish_choch": self.swing_bullish_choch,
            "swing_bearish_choch": self.swing_bearish_choch,
            "swing_point_type": self.swing_point_type,
            "broken_level": self.broken_level,
            "event_decision_timestamp": self.event_decision_timestamp,
            "pivot_candle_timestamp": self.pivot_candle_timestamp,
            "confirmation_timestamp": self.confirmation_timestamp,
            "internal_new_pivot_high": self.internal_new_pivot_high,
            "internal_new_pivot_low": self.internal_new_pivot_low,
            "swing_new_pivot_high": self.swing_new_pivot_high,
            "swing_new_pivot_low": self.swing_new_pivot_low,
        }


@dataclass
class LuxStructureEngine:
    """Causal LuxAlgo-style structure runner on a closed OHLCV frame.

    Frame columns: timestamp (bar open), open, high, low, close.
    ``decision_time`` column optional; default = open + bar duration.
    """

    timeframe: str
    internal_size: int = 5
    swing_size: int = 50
    filter_internal_confluence: bool = False

    internal_high: PivotState = field(default_factory=PivotState)
    internal_low: PivotState = field(default_factory=PivotState)
    swing_high: PivotState = field(default_factory=PivotState)
    swing_low: PivotState = field(default_factory=PivotState)
    internal_trend: TrendState = field(default_factory=TrendState)
    swing_trend: TrendState = field(default_factory=TrendState)
    _internal_leg: int = BEARISH_LEG
    _swing_leg: int = BEARISH_LEG
    _prev_internal_leg: int | None = None
    _prev_swing_leg: int | None = None

    def _update_leg(self, highs: np.ndarray, lows: np.ndarray, i: int, size: int, prev: int) -> int:
        leg = prev
        if new_leg_high(highs, i, size):
            leg = BEARISH_LEG
        elif new_leg_low(lows, i, size):
            leg = BULLISH_LEG
        return leg

    def _leg_change(self, prev: int | None, cur: int) -> int | None:
        if prev is None:
            return None
        return cur - prev

    def _apply_pivots(
        self,
        *,
        size: int,
        i: int,
        highs: np.ndarray,
        lows: np.ndarray,
        times: list[pd.Timestamp],
        leg: int,
        prev_leg: int | None,
        high_piv: PivotState,
        low_piv: PivotState,
        is_swing: bool,
    ) -> tuple[str, str | None, bool, bool]:
        """Return (swing_point_type, pivot_candle_ts, new_high, new_low)."""
        ch = self._leg_change(prev_leg, leg)
        new_high = ch == -1
        new_low = ch == +1
        pivot_ts: str | None = None
        point = ""
        if new_low:
            pivot_i = i - size
            level = float(lows[pivot_i])
            low_piv.last_level = low_piv.current_level
            low_piv.current_level = level
            low_piv.crossed = False
            low_piv.bar_time = times[pivot_i]
            low_piv.bar_index = pivot_i
            pivot_ts = _iso(times[pivot_i])
            if is_swing and low_piv.last_level is not None:
                point = "LL" if level < low_piv.last_level else "HL"
            elif is_swing:
                point = "HL"
        elif new_high:
            pivot_i = i - size
            level = float(highs[pivot_i])
            high_piv.last_level = high_piv.current_level
            high_piv.current_level = level
            high_piv.crossed = False
            high_piv.bar_time = times[pivot_i]
            high_piv.bar_index = pivot_i
            pivot_ts = _iso(times[pivot_i])
            if is_swing and high_piv.last_level is not None:
                point = "HH" if level > high_piv.last_level else "LH"
            elif is_swing:
                point = "LH"
        return point, pivot_ts, new_high, new_low

    def _detect_breaks(
        self,
        *,
        close: float,
        prior_close: float | None,
        high: float,
        low: float,
        open_: float,
        internal: bool,
    ) -> tuple[bool, bool, bool, bool, float | None]:
        """Bull BOS/CHoCH, Bear BOS/CHoCH, broken level.

        Uses Pine ``ta.crossover`` / ``ta.crossunder`` semantics vs active level:
        crossover ⇔ prior_close <= level and close > level (once; ``crossed`` latch).
        """
        bull_bos = bear_bos = bull_choch = bear_choch = False
        broken: float | None = None

        if internal and self.filter_internal_confluence:
            bullish_bar = (high - max(close, open_)) > (min(close, open_) - low)
            bearish_bar = (high - max(close, open_)) < (min(close, open_) - low)
        else:
            bullish_bar = True
            bearish_bar = True

        high_piv = self.internal_high if internal else self.swing_high
        low_piv = self.internal_low if internal else self.swing_low
        trend = self.internal_trend if internal else self.swing_trend

        # Bullish cross of active high
        if (
            high_piv.current_level is not None
            and not high_piv.crossed
            and prior_close is not None
            and prior_close <= high_piv.current_level
            and close > high_piv.current_level
        ):
            # Pine: not internal or (level != swingHigh and bullishBar)
            extra = True
            if internal:
                extra = (
                    high_piv.current_level != self.swing_high.current_level
                    and bullish_bar
                )
            if extra:
                tag_choch = trend.bias == BEARISH
                if tag_choch:
                    bull_choch = True
                else:
                    # bias 0 or BULLISH → BOS (LuxAlgo: only CHoCH when bias==BEARISH)
                    bull_bos = True
                high_piv.crossed = True
                trend.bias = BULLISH
                broken = high_piv.current_level

        # Bearish cross of active low
        if (
            low_piv.current_level is not None
            and not low_piv.crossed
            and prior_close is not None
            and prior_close >= low_piv.current_level
            and close < low_piv.current_level
        ):
            extra = True
            if internal:
                extra = (
                    low_piv.current_level != self.swing_low.current_level
                    and bearish_bar
                )
            if extra:
                tag_choch = trend.bias == BULLISH
                if tag_choch:
                    bear_choch = True
                else:
                    bear_bos = True
                low_piv.crossed = True
                trend.bias = BEARISH
                broken = low_piv.current_level if broken is None else broken

        return bull_bos, bear_bos, bull_choch, bear_choch, broken

    def run(self, frame: pd.DataFrame) -> list[StructureBar]:
        df = frame.copy()
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        if "decision_time" not in df.columns:
            raise ValueError("frame requires decision_time (closed-bar decision UTC)")
        df["decision_time"] = pd.to_datetime(df["decision_time"], utc=True)
        highs = df["high"].astype(float).to_numpy()
        lows = df["low"].astype(float).to_numpy()
        closes = df["close"].astype(float).to_numpy()
        opens = df["open"].astype(float).to_numpy()
        times = [_ts(t) for t in df["timestamp"]]
        decisions = [_ts(t) for t in df["decision_time"]]

        out: list[StructureBar] = []
        for i in range(len(df)):
            # legs
            self._internal_leg = self._update_leg(highs, lows, i, self.internal_size, self._internal_leg)
            self._swing_leg = self._update_leg(highs, lows, i, self.swing_size, self._swing_leg)

            swing_point = ""
            pivot_candle_ts = None
            conf_ts = None

            sp, pts, snh, snl = self._apply_pivots(
                size=self.swing_size,
                i=i,
                highs=highs,
                lows=lows,
                times=times,
                leg=self._swing_leg,
                prev_leg=self._prev_swing_leg,
                high_piv=self.swing_high,
                low_piv=self.swing_low,
                is_swing=True,
            )
            if snh or snl:
                swing_point = sp
                pivot_candle_ts = pts
                conf_ts = _iso(decisions[i])

            _, _, inh, inl = self._apply_pivots(
                size=self.internal_size,
                i=i,
                highs=highs,
                lows=lows,
                times=times,
                leg=self._internal_leg,
                prev_leg=self._prev_internal_leg,
                high_piv=self.internal_high,
                low_piv=self.internal_low,
                is_swing=False,
            )

            prior_close = float(closes[i - 1]) if i > 0 else None
            # BOS/CHoCH after pivots update (same bar order as LuxAlgo: structure then display)
            ib_bos, ib_bear, ib_choch, ib_bchoch, _ = self._detect_breaks(
                close=float(closes[i]),
                prior_close=prior_close,
                high=float(highs[i]),
                low=float(lows[i]),
                open_=float(opens[i]),
                internal=True,
            )
            sb_bos, sb_bear, sb_choch, sb_bchoch, broken = self._detect_breaks(
                close=float(closes[i]),
                prior_close=prior_close,
                high=float(highs[i]),
                low=float(lows[i]),
                open_=float(opens[i]),
                internal=False,
            )

            self._prev_internal_leg = self._internal_leg
            self._prev_swing_leg = self._swing_leg

            out.append(
                StructureBar(
                    timestamp_utc=_iso(times[i]) or "",
                    timeframe=self.timeframe,
                    close=float(closes[i]),
                    internal_leg=self._internal_leg,
                    swing_leg=self._swing_leg,
                    internal_pivot_high=self.internal_high.current_level,
                    internal_pivot_low=self.internal_low.current_level,
                    swing_pivot_high=self.swing_high.current_level,
                    swing_pivot_low=self.swing_low.current_level,
                    internal_pivot_high_timestamp=_iso(self.internal_high.bar_time),
                    internal_pivot_low_timestamp=_iso(self.internal_low.bar_time),
                    swing_pivot_high_timestamp=_iso(self.swing_high.bar_time),
                    swing_pivot_low_timestamp=_iso(self.swing_low.bar_time),
                    internal_bias=self.internal_trend.bias,
                    swing_bias=self.swing_trend.bias,
                    internal_bullish_bos=ib_bos,
                    internal_bearish_bos=ib_bear,
                    internal_bullish_choch=ib_choch,
                    internal_bearish_choch=ib_bchoch,
                    swing_bullish_bos=sb_bos,
                    swing_bearish_bos=sb_bear,
                    swing_bullish_choch=sb_choch,
                    swing_bearish_choch=sb_bchoch,
                    swing_point_type=swing_point,
                    broken_level=broken,
                    event_decision_timestamp=_iso(decisions[i]) or "",
                    pivot_candle_timestamp=pivot_candle_ts,
                    confirmation_timestamp=conf_ts,
                    internal_new_pivot_high=inh,
                    internal_new_pivot_low=inl,
                    swing_new_pivot_high=snh,
                    swing_new_pivot_low=snl,
                )
            )
        return out


def run_lux_structure(
    frame: pd.DataFrame,
    *,
    timeframe: str,
    internal_size: int = 5,
    swing_size: int = 50,
) -> list[dict[str, Any]]:
    eng = LuxStructureEngine(
        timeframe=timeframe,
        internal_size=internal_size,
        swing_size=swing_size,
    )
    return [b.to_dict() for b in eng.run(frame)]

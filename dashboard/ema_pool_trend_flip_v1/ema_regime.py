"""Stateful EMA9/EMA20/ATR14 regime on closed signal-timeframe bars. No lookahead."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .config import (
    ATR_PERIOD,
    EMA_CROSS_CONFIRMATION_BARS,
    EMA_CROSS_MIN_SEPARATION_ATR,
    EMA_FAST,
    EMA_GAP_GROWTH_BARS,
    EMA_SLOW,
)


def _ema(values: Sequence[float], span: int) -> list[float | None]:
    if span <= 0:
        raise ValueError("span")
    out: list[float | None] = []
    alpha = 2.0 / (span + 1.0)
    prev: float | None = None
    for v in values:
        if prev is None:
            prev = float(v)
        else:
            prev = alpha * float(v) + (1.0 - alpha) * prev
        out.append(prev)
    return out


def _atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int) -> list[float | None]:
    n = len(closes)
    trs: list[float] = []
    for i in range(n):
        h = float(highs[i])
        l = float(lows[i])
        prev_c = float(closes[i - 1]) if i else float(closes[i])
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    out: list[float | None] = [None] * n
    if n < period:
        return out
    seed = sum(trs[:period]) / float(period)
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = (prev * (period - 1) + trs[i]) / float(period)
        out[i] = prev
    return out


@dataclass
class BarIndicators:
    ema9: float
    ema20: float
    atr14: float
    close: float
    ema9_rising: bool
    ema20_rising: bool
    gap_growing: bool
    sep_atr: float
    side: str  # "ABOVE" | "BELOW" | "TOUCH"


def indicators_for_bars(
    closes: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    *,
    gap_bars: int = EMA_GAP_GROWTH_BARS,
) -> list[BarIndicators | None]:
    e9 = _ema(closes, EMA_FAST)
    e20 = _ema(closes, EMA_SLOW)
    atr = _atr(highs, lows, closes, ATR_PERIOD)
    out: list[BarIndicators | None] = []
    for i in range(len(closes)):
        a9, a20, a = e9[i], e20[i], atr[i]
        if a9 is None or a20 is None or a is None or a <= 0:
            out.append(None)
            continue
        prev9 = e9[i - 1] if i else a9
        prev20 = e20[i - 1] if i else a20
        gap = abs(a9 - a20)
        growing = True
        if i >= gap_bars:
            prev_gap = abs(float(e9[i - gap_bars]) - float(e20[i - gap_bars]))
            growing = gap > prev_gap + 1e-12
        if a9 > a20:
            side = "ABOVE"
        elif a9 < a20:
            side = "BELOW"
        else:
            side = "TOUCH"
        out.append(
            BarIndicators(
                ema9=float(a9),
                ema20=float(a20),
                atr14=float(a),
                close=float(closes[i]),
                ema9_rising=float(a9) > float(prev9 or a9),
                ema20_rising=float(a20) > float(prev20 or a20),
                gap_growing=growing,
                sep_atr=gap / float(a),
                side=side,
            )
        )
    return out


def confirmed_strong_crosses(
    inds: Sequence[BarIndicators | None],
    *,
    confirm_bars: int = EMA_CROSS_CONFIRMATION_BARS,
    min_sep: float = EMA_CROSS_MIN_SEPARATION_ATR,
) -> list[dict[str, Any]]:
    """Cross is recognized on the last confirmation bar (index). Fill is next bar open."""
    events: list[dict[str, Any]] = []
    n = len(inds)
    i = confirm_bars
    while i < n:
        cur = inds[i]
        if cur is None:
            i += 1
            continue
        window = inds[i - confirm_bars + 1 : i + 1]
        if any(w is None for w in window):
            i += 1
            continue
        sides = [w.side for w in window]
        if all(s == "ABOVE" for s in sides) and cur.sep_atr + 1e-12 >= min_sep and cur.ema9_rising and cur.close > cur.ema9 and cur.close > cur.ema20:
            prev = inds[i - confirm_bars]
            if prev is not None and prev.side != "ABOVE":
                events.append({"index": i, "kind": "CONFIRMED_STRONG_BULLISH_EMA_CROSS", "sep_atr": cur.sep_atr})
                i += 1
                continue
        if all(s == "BELOW" for s in sides) and cur.sep_atr + 1e-12 >= min_sep and (not cur.ema9_rising) and cur.close < cur.ema9 and cur.close < cur.ema20:
            prev = inds[i - confirm_bars]
            if prev is not None and prev.side != "BELOW":
                events.append({"index": i, "kind": "CONFIRMED_STRONG_BEARISH_EMA_CROSS", "sep_atr": cur.sep_atr})
                i += 1
                continue
        i += 1
    return events


def weak_cross_candidates(inds: Sequence[BarIndicators | None]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(1, len(inds)):
        a, b = inds[i - 1], inds[i]
        if a is None or b is None:
            continue
        if a.side != b.side and (a.side != "TOUCH" or b.side != "TOUCH"):
            out.append({"index": i, "kind": "WEAK_CROSS_CANDIDATE", "from": a.side, "to": b.side, "sep_atr": b.sep_atr})
        elif a.side != "TOUCH" and b.side == "TOUCH":
            out.append({"index": i, "kind": "EMA_TOUCH", "from": a.side, "to": b.side, "sep_atr": b.sep_atr})
    return out


def last_confirmed_kind(events: Sequence[dict[str, Any]], as_of_index: int) -> str | None:
    last = None
    for ev in events:
        if int(ev["index"]) <= as_of_index:
            last = str(ev["kind"])
    return last


def unique_uptrend(ind: BarIndicators, last_kind: str | None) -> bool:
    return (
        last_kind == "CONFIRMED_STRONG_BULLISH_EMA_CROSS"
        and ind.side == "ABOVE"
        and ind.ema9_rising
        and ind.ema20_rising
        and ind.gap_growing
        and ind.close > ind.ema9
        and ind.close > ind.ema20
    )


def unique_downtrend(ind: BarIndicators, last_kind: str | None) -> bool:
    return (
        last_kind == "CONFIRMED_STRONG_BEARISH_EMA_CROSS"
        and ind.side == "BELOW"
        and (not ind.ema9_rising)
        and (not ind.ema20_rising)
        and ind.gap_growing
        and ind.close < ind.ema9
        and ind.close < ind.ema20
    )


def regime_at_index(
    inds: Sequence[BarIndicators | None],
    events: Sequence[dict[str, Any]],
    index: int,
) -> dict[str, Any]:
    ind = inds[index] if 0 <= index < len(inds) else None
    last = last_confirmed_kind(events, index)
    if ind is None:
        return {
            "ema_trend": "NONE",
            "last_confirmed_cross": last,
            "ema9": None,
            "ema20": None,
            "sep_atr": None,
            "unique_up": False,
            "unique_down": False,
        }
    up = unique_uptrend(ind, last)
    down = unique_downtrend(ind, last)
    trend = "UP" if up else ("DOWN" if down else "NONE")
    return {
        "ema_trend": trend,
        "last_confirmed_cross": last,
        "ema9": ind.ema9,
        "ema20": ind.ema20,
        "atr14": ind.atr14,
        "sep_atr": ind.sep_atr,
        "unique_up": up,
        "unique_down": down,
        "close": ind.close,
    }

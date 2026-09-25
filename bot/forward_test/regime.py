"""Isolated higher-timeframe regime filter.

A short signal asks this module first. If 1h or 4h is bullish
(EMA9 and EMA20 both above EMA59), the signal is ignored.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


def stack_label(ema9: float, ema20: float, ema59: float) -> str:
    """Bullish when both fast EMAs sit above EMA59, bearish when both sit below."""
    if ema9 > ema59 and ema20 > ema59:
        return "bullish"
    if ema9 < ema59 and ema20 < ema59:
        return "bearish"
    return "neutral"


def combine_regimes(h1: str, h4: str) -> str:
    """Bullish if either timeframe is bullish. Bearish only when both are."""
    if h1 == "bullish" or h4 == "bullish":
        return "bullish"
    if h1 == "bearish" and h4 == "bearish":
        return "bearish"
    return "neutral"


def allows_short(regime: str) -> bool:
    """Shorts pass in bearish and mixed regimes. Bullish and unknown are ignored."""
    return regime in {"bearish", "neutral"}


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def ema_last(closes: list[float], period: int) -> float | None:
    if len(closes) < period:
        return None
    k = 2.0 / (period + 1.0)
    value = sum(closes[:period]) / period
    for px in closes[period:]:
        value = px * k + value * (1.0 - k)
    return float(value)


def _floor_hours(ts: datetime, hours: int) -> datetime:
    ts = _utc(ts)
    hour = (ts.hour // hours) * hours
    return ts.replace(hour=hour, minute=0, second=0, microsecond=0)


def aggregate_closes(bars: list[Any], *, hours: int) -> list[tuple[datetime, float]]:
    """Complete HTF candles only. A 1h bucket needs 12 closed 5m bars, 4h needs 48."""
    need = hours * 12
    buckets: dict[datetime, list[Any]] = {}
    for bar in bars:
        key = _floor_hours(bar.ts, hours)
        buckets.setdefault(key, []).append(bar)
    out: list[tuple[datetime, float]] = []
    for key in sorted(buckets):
        group = sorted(buckets[key], key=lambda b: _utc(b.ts))
        if len(group) < need:
            continue
        out.append((key, float(group[-1].close)))
    return out


def _tf_state(bars: list[Any], hours: int) -> dict[str, Any]:
    candles = aggregate_closes(bars, hours=hours)
    closes = [px for _ts, px in candles]
    ema9 = ema_last(closes, 9)
    ema20 = ema_last(closes, 20)
    ema59 = ema_last(closes, 59)
    if ema9 is None or ema20 is None or ema59 is None or not closes:
        return {
            "label": "unknown",
            "close": closes[-1] if closes else None,
            "ema9": ema9,
            "ema20": ema20,
            "ema59": ema59,
            "bars": len(closes),
        }
    return {
        "label": stack_label(ema9, ema20, ema59),
        "close": closes[-1],
        "ema9": ema9,
        "ema20": ema20,
        "ema59": ema59,
        "bars": len(closes),
    }


def market_regime(symbol: str, now: datetime | None = None) -> dict[str, Any]:
    """Classify 1h and 4h from closed 5m public-trade bars. No lookahead."""
    from bot.forward_test.config import REGIME_LOOKBACK_DAYS
    from bot.forward_test.paths import ensure_import_paths

    ensure_import_paths()
    from ob_microstructure_breakout_bot.data.bars import load_5m_bars

    as_of = _utc(now or datetime.now(timezone.utc))
    bars = load_5m_bars(symbol, as_of - timedelta(days=REGIME_LOOKBACK_DAYS), as_of)
    h1 = _tf_state(bars, 1)
    h4 = _tf_state(bars, 4)
    if h1["label"] == "unknown" or h4["label"] == "unknown":
        regime = "unknown"
    else:
        regime = combine_regimes(str(h1["label"]), str(h4["label"]))
    return {
        "symbol": symbol.upper(),
        "as_of": as_of.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "regime": regime,
        "allows_short": allows_short(regime),
        "h1": h1,
        "h4": h4,
    }

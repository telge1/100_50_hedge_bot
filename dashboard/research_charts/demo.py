"""Placeholder OHLCV for Research Charts Phase 1.

Not a second aggregation/stochastic engine. Phase 2 must load candles via a
DataSource and call trading_research_platform.data.timeframes.aggregate.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from .boundary import SUPPORTED_TIMEFRAMES

_TF_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
}

_DEMO_SYMBOLS = (
    {"symbol": "APTUSDT", "timeframes": list(SUPPORTED_TIMEFRAMES)},
    {"symbol": "DOGEUSDT", "timeframes": list(SUPPORTED_TIMEFRAMES)},
    {"symbol": "HYPEUSDT", "timeframes": list(SUPPORTED_TIMEFRAMES)},
)


def demo_symbols() -> list[dict]:
    return [dict(row) for row in _DEMO_SYMBOLS]


def demo_candles(
    symbol: str,
    timeframe: str,
    *,
    limit: int = 180,
    end_ts: int | None = None,
) -> list[dict]:
    tf = timeframe if timeframe in _TF_SECONDS else "5m"
    step = _TF_SECONDS[tf]
    n = max(20, min(int(limit or 180), 500))
    end = int(end_ts or datetime.now(timezone.utc).timestamp())
    end = (end // step) * step
    seed = sum(ord(ch) for ch in (symbol or "DEMO").upper()) % 97
    base = 5.0 if "APT" in (symbol or "").upper() else 0.08 if "DOGE" in (symbol or "").upper() else 40.0
    out: list[dict] = []
    price = base
    for i in range(n):
        t = end - (n - 1 - i) * step
        drift = math.sin((i + seed) / 9.0) * base * 0.004
        noise = math.sin((i + seed) * 0.37) * base * 0.0015
        o = price
        c = max(base * 0.5, o + drift + noise)
        h = max(o, c) + abs(noise)
        l = min(o, c) - abs(noise)
        vol = 1000.0 + (i % 17) * 40.0 + seed
        out.append(
            {
                "time": int(t),
                "open": round(o, 6),
                "high": round(h, 6),
                "low": round(l, 6),
                "close": round(c, 6),
                "volume": round(vol, 4),
            }
        )
        price = c
    return out

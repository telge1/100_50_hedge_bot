"""Load closed 1m bars from ClickHouse (read-only, same path as research)."""

from __future__ import annotations

from datetime import datetime, timezone

from bot.long_v1_live_scanner.config import ensure_runtime_paths


def load_1m_bars(symbol: str, start: datetime, end: datetime) -> list[dict]:
    ensure_runtime_paths()
    from dashboard.research_charts.service import _candles_from_packed, load_candles
    from pool_scan.clock import bar_close

    start_u = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
    end_u = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
    packed = load_candles(
        symbol,
        "1m",
        start=int(start_u.timestamp()),
        end=int(end_u.timestamp()),
        limit=500_000,
    )
    candles = _candles_from_packed(packed)
    bars: list[dict] = []
    for idx, candle in enumerate(candles):
        ot = candle.timestamp
        if ot.tzinfo is None:
            ot = ot.replace(tzinfo=timezone.utc)
        bars.append(
            {
                "open_time": ot,
                "close_time": bar_close(ot, "1m"),
                "open": float(candle.open),
                "high": float(candle.high),
                "low": float(candle.low),
                "close": float(candle.close),
                "candle_index": idx,
            }
        )
    return bars

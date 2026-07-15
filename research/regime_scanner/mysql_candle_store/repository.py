"""Read-only candle repository API for a future scanner MySQL switch."""

from __future__ import annotations

from typing import Any

import pandas as pd

from research.regime_scanner.mysql_candle_store.store_memory import CandleStore


def load_candles(
    store: CandleStore,
    exchange: str,
    symbol: str,
    timeframe: str,
    start_time: object | None = None,
    end_time: object | None = None,
    decision_time: object | None = None,
    closed_only: bool = True,
    source: str | None = None,
) -> pd.DataFrame:
    """Load sorted candles; when ``decision_time`` is set, require ``close_time <= decision_time``.

    ``source=None`` returns all operational candles for the key (Direct and Aggregated).
    HTF buckets beyond the 5m series end are included when present.
    """
    frame = store.fetch_candles(
        exchange=exchange,
        symbol=symbol,
        timeframe=timeframe,
        start_time=start_time,
        end_time=end_time,
        decision_time=decision_time,
        closed_only=closed_only,
        source=source,
    )
    if frame.empty:
        return frame
    preferred = [
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "open_time",
        "close_time",
        "is_closed",
        "source",
        "source_timeframe",
    ]
    cols = [c for c in preferred if c in frame.columns] + [
        c for c in frame.columns if c not in preferred
    ]
    out = frame.loc[:, cols].copy()
    if out["timestamp"].duplicated().any():
        raise ValueError("duplicate timestamps in repository load result")
    if not bool(out["timestamp"].is_monotonic_increasing):
        raise ValueError("timestamps not sorted ascending")
    return out.reset_index(drop=True)


def summarize_timeframe(
    store: CandleStore,
    *,
    exchange: str,
    symbol: str,
    timeframe: str,
) -> dict[str, Any]:
    frame = load_candles(store, exchange, symbol, timeframe, closed_only=True)
    if frame.empty:
        return {
            "timeframe": timeframe,
            "rows": 0,
            "min_open_time": None,
            "max_open_time": None,
            "sources": {},
        }
    sources = (
        frame["source"].value_counts().to_dict() if "source" in frame.columns else {}
    )
    return {
        "timeframe": timeframe,
        "rows": int(len(frame)),
        "min_open_time": str(frame["timestamp"].iloc[0]),
        "max_open_time": str(frame["timestamp"].iloc[-1]),
        "sources": {str(k): int(v) for k, v in sources.items()},
    }

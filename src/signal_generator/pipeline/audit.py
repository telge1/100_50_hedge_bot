"""HTF aggregation + signal parity helpers for shadow audits."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

import pandas as pd

from signal_generator.timeframes import (
    OhlcvBar,
    aggregate_bucket,
    bars_from_mappings,
    bucket_close,
    bucket_start,
    ensure_utc,
    expected_1m_count,
)


def manual_aggregate_1m(
    candles_1m: Sequence[dict[str, Any]] | Sequence[OhlcvBar],
    *,
    timeframe: str,
    bucket_open: datetime,
) -> dict[str, Any] | None:
    """Hand-roll OHLCV for one complete bucket (audit reference)."""
    start = bucket_start(ensure_utc(bucket_open), timeframe)
    close_t = bucket_close(start, timeframe)
    n = expected_1m_count(timeframe)
    if candles_1m and isinstance(candles_1m[0], OhlcvBar):
        bars = list(candles_1m)  # type: ignore[arg-type]
    else:
        bars = bars_from_mappings(candles_1m)  # type: ignore[arg-type]
    by_ot = {ensure_utc(b.open_time): b for b in bars}
    members = []
    for i in range(n):
        ot = start + timedelta(minutes=i)
        if ot not in by_ot:
            return None
        members.append(by_ot[ot])
    return {
        "bucket_open": start,
        "close_time": close_t,
        "available_at": close_t,
        "open": float(members[0].open),
        "high": max(float(m.high) for m in members),
        "low": min(float(m.low) for m in members),
        "close": float(members[-1].close),
        "volume": sum(float(m.volume) for m in members),
    }


def htf_bar_matches_manual(
    candles_1m: Sequence[dict[str, Any]],
    *,
    timeframe: str,
    bucket_open: datetime,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    bars = bars_from_mappings(candles_1m)
    start = bucket_start(ensure_utc(bucket_open), timeframe)
    as_of = ensure_utc(as_of or bucket_close(start, timeframe))
    bar = aggregate_bucket(bars, timeframe=timeframe, bucket_open=start, as_of=as_of)
    manual = manual_aggregate_1m(bars, timeframe=timeframe, bucket_open=start)
    if bar is None or manual is None:
        return {"ok": False, "reason": "incomplete_or_not_closed", "timeframe": timeframe}
    checks = {
        "open": abs(bar.open - manual["open"]) < 1e-9,
        "high": abs(bar.high - manual["high"]) < 1e-9,
        "low": abs(bar.low - manual["low"]) < 1e-9,
        "close": abs(bar.close - manual["close"]) < 1e-9,
        "volume": abs(bar.volume - manual["volume"]) < 1e-9,
        "bucket_open": ensure_utc(bar.open_time) == manual["bucket_open"],
        "close_time": ensure_utc(bar.close_time) == manual["close_time"],
        "available_at": ensure_utc(bar.available_at) == manual["available_at"],
    }
    return {
        "ok": all(checks.values()),
        "timeframe": timeframe,
        "bucket_open": start.isoformat(),
        "checks": checks,
        "pipeline": {
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "available_at": bar.available_at.isoformat(),
        },
        "manual": {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in manual.items()},
    }


def signal_fingerprint(row: dict[str, Any]) -> tuple:
    return (
        str(row["symbol"]),
        str(row["timeframe"]),
        str(row["direction"]),
        str(row["signal_id"]),
        pd.Timestamp(row["candle_open_time"]).isoformat(),
        int(row["tier_a"]),
    )

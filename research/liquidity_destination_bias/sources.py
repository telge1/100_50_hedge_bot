"""Bounded read-only public-trade source for Phase 1."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from research.btc_doge_research.clickhouse import connect, rows

from .models import PriceBucket, utc

READ_TABLE = "btc_doge_research.research_public_trades"


@dataclass(frozen=True)
class TradeSecondSeries:
    buckets: dict[datetime, PriceBucket]
    coverage_start: datetime | None
    coverage_end: datetime | None
    source_complete: bool = True

    def price_before(self, t0: datetime) -> float | None:
        t0_u = utc(t0)
        candidates = [ts for ts in self.buckets if ts < t0_u]
        return self.buckets[max(candidates)].close if candidates else None

    def window(self, start: datetime, end: datetime) -> list[PriceBucket]:
        start_u, end_u = utc(start), utc(end)
        return [
            self.buckets[ts]
            for ts in sorted(self.buckets)
            if start_u <= ts < end_u
        ]

    def covers(self, start: datetime, end: datetime) -> bool:
        """Boundary coverage; empty trade seconds are valid."""
        start_u, end_u = utc(start), utc(end)
        return bool(
            self.coverage_start is not None
            and self.coverage_end is not None
            and self.coverage_start <= start_u
            and self.coverage_end >= end_u
        )

    def coverage_complete(self, start: datetime, end: datetime) -> bool:
        return self.source_complete and self.covers(start, end)


def _assert_bounded(start: datetime, end: datetime, max_seconds: int) -> None:
    seconds = (utc(end) - utc(start)).total_seconds()
    if seconds <= 0 or seconds > max_seconds:
        raise ValueError(f"read window must be in (0,{max_seconds}] seconds")


def load_trade_seconds(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    client: Any | None = None,
    max_seconds: int = 3 * 60 * 60 + 10 * 60,
) -> TradeSecondSeries:
    """Aggregate deduplicated event-time trades to bounded 1s OHLC rows."""
    start_u, end_u = utc(start), utc(end)
    _assert_bounded(start_u, end_u, max_seconds)
    db = client or connect()
    sql = f"""
        SELECT
            toStartOfSecond(event_time) AS bucket_start,
            min(toFloat64(price)) AS low,
            max(toFloat64(price)) AS high,
            argMax(toFloat64(price), tuple(event_time, trade_id)) AS close,
            count() AS trade_count,
            countIf(coverage_status != 'COMPLETE') AS incomplete_count
        FROM {READ_TABLE} FINAL
        WHERE symbol = %(symbol)s
          AND event_time >= %(start)s
          AND event_time < %(end)s
        GROUP BY bucket_start
        ORDER BY bucket_start
    """
    result = rows(db, sql, {"symbol": symbol, "start": start_u, "end": end_u})
    buckets: dict[datetime, PriceBucket] = {}
    incomplete_count = 0
    for bucket_start, low, high, close, count, incomplete in result:
        ts = utc(bucket_start.replace(tzinfo=bucket_start.tzinfo) if bucket_start.tzinfo else bucket_start.replace(tzinfo=start_u.tzinfo))
        buckets[ts] = PriceBucket(ts, float(low), float(high), float(close), int(count))
        incomplete_count += int(incomplete)
    return TradeSecondSeries(
        buckets=buckets,
        coverage_start=min(buckets) if buckets else None,
        coverage_end=(max(buckets) + timedelta(seconds=1)) if buckets else None,
        source_complete=incomplete_count == 0,
    )

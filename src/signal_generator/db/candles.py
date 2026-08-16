"""Repository for canonical closed 1m candles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Sequence

from signal_generator.db.client import ClickHouseClient

CANDLE_COLUMNS = [
    "exchange",
    "symbol",
    "interval",
    "open_time",
    "close_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
    "is_closed",
    "source",
    "source_event_time",
    "ingested_at",
]


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


@dataclass(slots=True)
class Candle1m:
    exchange: str
    symbol: str
    open_time: datetime
    close_time: datetime
    open: Decimal | float | str
    high: Decimal | float | str
    low: Decimal | float | str
    close: Decimal | float | str
    volume: Decimal | float | str
    turnover: Decimal | float | str
    source: str
    interval: str = "1m"
    is_closed: bool = True
    source_event_time: datetime | None = None
    ingested_at: datetime | None = None

    def as_row(self) -> list[Any]:
        ingested = self.ingested_at or datetime.now(timezone.utc)
        return [
            self.exchange,
            self.symbol,
            self.interval,
            _ensure_utc(self.open_time),
            _ensure_utc(self.close_time),
            self.open,
            self.high,
            self.low,
            self.close,
            self.volume,
            self.turnover,
            1 if self.is_closed else 0,
            self.source,
            _ensure_utc(self.source_event_time) if self.source_event_time else None,
            _ensure_utc(ingested),
        ]


class CandleRepository:
    TABLE = "candles_1m"

    def __init__(self, client: ClickHouseClient) -> None:
        self._client = client

    def insert_candles(self, candles: Sequence[Candle1m]) -> int:
        """Batch-insert candles. Empty input is a no-op. Returns row count sent."""
        if not candles:
            return 0
        rows = [c.as_row() for c in candles]
        self._client.insert(self.TABLE, rows, CANDLE_COLUMNS)
        return len(rows)

    def get_candles(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        exchange: str = "bybit",
        interval: str = "1m",
    ) -> list[dict[str, Any]]:
        """Return deduplicated closed candles for one symbol in [start, end).

        Uses FINAL so ReplacingMergeTree duplicates do not distort analysis.
        """
        start = _ensure_utc(start)
        end = _ensure_utc(end)
        result = self._client.query(
            f"""
            SELECT
                exchange, symbol, interval, open_time, close_time,
                open, high, low, close, volume, turnover,
                is_closed, source, source_event_time, ingested_at
            FROM {self._client.database}.{self.TABLE} FINAL
            WHERE exchange = {{exchange:String}}
              AND symbol = {{symbol:String}}
              AND interval = {{interval:String}}
              AND open_time >= {{start:DateTime64(3, 'UTC')}}
              AND open_time < {{end:DateTime64(3, 'UTC')}}
              AND is_closed = 1
            ORDER BY open_time ASC
            """,
            parameters={
                "exchange": exchange,
                "symbol": symbol,
                "interval": interval,
                "start": start,
                "end": end,
            },
        )
        return [dict(zip(result.column_names, row, strict=True)) for row in result.result_rows]

    def get_candles_multi(
        self,
        symbols: Sequence[str],
        start: datetime,
        end: datetime,
        *,
        exchange: str = "bybit",
        interval: str = "1m",
    ) -> list[dict[str, Any]]:
        """Return deduplicated candles for multiple symbols in [start, end)."""
        if not symbols:
            return []
        start = _ensure_utc(start)
        end = _ensure_utc(end)
        result = self._client.query(
            f"""
            SELECT
                exchange, symbol, interval, open_time, close_time,
                open, high, low, close, volume, turnover,
                is_closed, source, source_event_time, ingested_at
            FROM {self._client.database}.{self.TABLE} FINAL
            WHERE exchange = {{exchange:String}}
              AND symbol IN {{symbols:Array(String)}}
              AND interval = {{interval:String}}
              AND open_time >= {{start:DateTime64(3, 'UTC')}}
              AND open_time < {{end:DateTime64(3, 'UTC')}}
              AND is_closed = 1
            ORDER BY symbol ASC, open_time ASC
            """,
            parameters={
                "exchange": exchange,
                "symbols": list(symbols),
                "interval": interval,
                "start": start,
                "end": end,
            },
        )
        return [dict(zip(result.column_names, row, strict=True)) for row in result.result_rows]

    def count_final(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        exchange: str = "bybit",
        interval: str = "1m",
    ) -> int:
        """Logical row count after ReplacingMergeTree dedup (FINAL)."""
        start = _ensure_utc(start)
        end = _ensure_utc(end)
        result = self._client.query(
            f"""
            SELECT count()
            FROM {self._client.database}.{self.TABLE} FINAL
            WHERE exchange = {{exchange:String}}
              AND symbol = {{symbol:String}}
              AND interval = {{interval:String}}
              AND open_time >= {{start:DateTime64(3, 'UTC')}}
              AND open_time < {{end:DateTime64(3, 'UTC')}}
            """,
            parameters={
                "exchange": exchange,
                "symbol": symbol,
                "interval": interval,
                "start": start,
                "end": end,
            },
        )
        return int(result.result_rows[0][0])

    def count_physical(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        exchange: str = "bybit",
        interval: str = "1m",
    ) -> int:
        """Physical row count without FINAL (may include unmerged duplicates)."""
        start = _ensure_utc(start)
        end = _ensure_utc(end)
        result = self._client.query(
            f"""
            SELECT count()
            FROM {self._client.database}.{self.TABLE}
            WHERE exchange = {{exchange:String}}
              AND symbol = {{symbol:String}}
              AND interval = {{interval:String}}
              AND open_time >= {{start:DateTime64(3, 'UTC')}}
              AND open_time < {{end:DateTime64(3, 'UTC')}}
            """,
            parameters={
                "exchange": exchange,
                "symbol": symbol,
                "interval": interval,
                "start": start,
                "end": end,
            },
        )
        return int(result.result_rows[0][0])

    def get_last_closed_open_time(
        self,
        symbol: str,
        *,
        exchange: str = "bybit",
        interval: str = "1m",
    ) -> datetime | None:
        """Latest closed candle open_time for symbol (FINAL), or None if empty."""
        result = self._client.query(
            f"""
            SELECT max(open_time)
            FROM {self._client.database}.{self.TABLE} FINAL
            WHERE exchange = {{exchange:String}}
              AND symbol = {{symbol:String}}
              AND interval = {{interval:String}}
              AND is_closed = 1
            """,
            parameters={
                "exchange": exchange,
                "symbol": symbol,
                "interval": interval,
            },
        )
        value = result.result_rows[0][0]
        if value is None:
            return None
        # ClickHouse may return epoch sentinel for empty max()
        if isinstance(value, datetime) and value.year <= 1970:
            return None
        return _ensure_utc(value)

    def delete_test_rows(self, *, source_prefix: str = "TEST_") -> int:
        """Delete rows whose source starts with prefix. For smoke cleanup only."""
        self._client.command(
            f"""
            ALTER TABLE {self._client.database}.{self.TABLE}
            DELETE WHERE startsWith(source, {{prefix:String}})
            """,
            parameters={"prefix": source_prefix},
        )
        return 0

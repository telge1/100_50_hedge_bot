"""Allowlisted ClickHouse writer. Refuses orderbook/trade/candle/signal tables."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Any, Callable, Protocol

from . import ALLOWED_TABLES, FORBIDDEN_TABLES

logger = logging.getLogger(__name__)

TABLE_COLUMNS: dict[str, list[str]] = {
    "all_liquidations": [
        "exchange",
        "category",
        "symbol",
        "event_time",
        "system_generated_at",
        "received_at",
        "position_side_raw",
        "liquidated_position_side",
        "size",
        "bankruptcy_price",
        "notional_estimate",
        "source_topic",
        "event_key",
        "raw_payload_hash",
        "collector_instance_id",
        "inserted_at",
    ],
    "open_interest_events": [
        "exchange",
        "category",
        "symbol",
        "event_time",
        "received_at",
        "cross_sequence",
        "open_interest",
        "open_interest_value",
        "single_open_interest",
        "single_open_interest_value",
        "last_price",
        "mark_price",
        "index_price",
        "funding_rate",
        "message_type",
        "source_topic",
        "state_valid",
        "event_key",
        "collector_instance_id",
        "inserted_at",
    ],
    "open_interest_5s": [
        "exchange",
        "category",
        "symbol",
        "bucket_time",
        "source_event_time",
        "received_at",
        "open_interest",
        "open_interest_value",
        "single_open_interest",
        "single_open_interest_value",
        "last_price",
        "mark_price",
        "index_price",
        "state_age_ms",
        "state_valid",
        "source",
        "collector_instance_id",
        "inserted_at",
    ],
    "open_interest_5m_history": [
        "exchange",
        "category",
        "symbol",
        "bucket_time",
        "open_interest",
        "open_interest_value",
        "source",
        "collector_instance_id",
        "inserted_at",
    ],
    "oi_liquidation_health": [
        "event_ts",
        "collector_instance_id",
        "symbol",
        "source",
        "event_type",
        "ws_connected",
        "ping_ok",
        "subscription_confirmed",
        "oi_state_valid",
        "oi_state_age_ms",
        "last_event_time",
        "last_received_at",
        "last_liquidation_time",
        "lag_ms",
        "messages_received",
        "rows_inserted",
        "duplicates_suppressed",
        "parse_errors",
        "insert_errors",
        "reconnect_count",
        "subscription_count",
        "queue_size",
        "queue_drops",
        "clock_offset_ms",
        "message",
    ],
}


class InsertError(RuntimeError):
    pass


class ClickHouseClient(Protocol):
    def insert(self, table: str, data: list[tuple[Any, ...]], column_names: list[str]) -> Any: ...
    def command(self, sql: str) -> Any: ...
    def query(self, sql: str) -> Any: ...
    def close(self) -> None: ...


def row_tuple(table: str, rec: dict[str, Any]) -> tuple[Any, ...]:
    cols = TABLE_COLUMNS[table]
    return tuple(rec.get(c) for c in cols)


def assert_table_allowed(table: str) -> None:
    if table in FORBIDDEN_TABLES or table not in ALLOWED_TABLES:
        raise ValueError(f"refusing write to table {table!r}")


class AllowlistedWriter:
    def __init__(
        self,
        *,
        client_factory: Callable[[], ClickHouseClient],
        batch_size: int = 500,
        flush_interval_sec: float = 1.0,
        queue_maxsize: int = 20_000,
        max_retries: int = 4,
    ) -> None:
        self._client_factory = client_factory
        self.batch_size = batch_size
        self.flush_interval_sec = flush_interval_sec
        self.max_retries = max_retries
        self._queue: asyncio.Queue[tuple[str, list[tuple[Any, ...]]] | None] = asyncio.Queue(
            maxsize=queue_maxsize
        )
        self._buffers: dict[str, list[tuple[Any, ...]]] = defaultdict(list)
        self._client: ClickHouseClient | None = None
        self.rows_inserted = 0
        self.insert_errors = 0
        self.queue_drops = 0
        self._worker: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._fatal: BaseException | None = None

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    async def start(self) -> None:
        self._stop.clear()
        self._worker = asyncio.create_task(self._run(), name="oi-liq-writer")

    async def enqueue(self, table: str, recs: list[dict[str, Any]]) -> int:
        assert_table_allowed(table)
        if not recs:
            return 0
        rows = [row_tuple(table, r) for r in recs]
        try:
            self._queue.put_nowait((table, rows))
            return 0
        except asyncio.QueueFull:
            self.queue_drops += len(rows)
            return len(rows)

    async def stop(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        if self._worker:
            await self._worker
            self._worker = None
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                logger.exception("close clickhouse")
            self._client = None
        if self._fatal:
            raise InsertError(str(self._fatal)) from self._fatal

    @staticmethod
    def _qualified(table: str) -> str:
        assert_table_allowed(table)
        return f"orderbook_analysis.{table}"

    def _insert_sync(self, table: str, rows: list[tuple[Any, ...]]) -> None:
        assert_table_allowed(table)
        if self._client is None:
            self._client = self._client_factory()
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                self._client.insert(self._qualified(table), rows, column_names=TABLE_COLUMNS[table])
                self.rows_inserted += len(rows)
                return
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                self.insert_errors += 1
                time.sleep(min(2**attempt, 8) * 0.05)
        raise InsertError(f"insert failed for {table}: {last_exc}") from last_exc

    async def _flush_table(self, table: str) -> None:
        chunk = self._buffers[table]
        if not chunk:
            return
        self._buffers[table] = []
        try:
            await asyncio.to_thread(self._insert_sync, table, chunk)
        except Exception:
            self._buffers[table] = chunk + self._buffers[table]
            raise

    async def _flush_all(self) -> None:
        for table in list(self._buffers):
            if self._buffers[table]:
                await self._flush_table(table)

    async def _run(self) -> None:
        last = time.monotonic()
        try:
            while not self._stop.is_set() or not self._queue.empty() or any(self._buffers.values()):
                timeout = max(0.01, self.flush_interval_sec - (time.monotonic() - last))
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    await self._flush_all()
                    last = time.monotonic()
                    continue
                if item is None:
                    await self._flush_all()
                    last = time.monotonic()
                    if self._stop.is_set():
                        break
                    continue
                table, rows = item
                self._buffers[table].extend(rows)
                if len(self._buffers[table]) >= self.batch_size:
                    await self._flush_table(table)
                    last = time.monotonic()
        except Exception as exc:  # noqa: BLE001
            self._fatal = exc
            logger.exception("writer failed")
        finally:
            try:
                await self._flush_all()
            except Exception as exc:  # noqa: BLE001
                self._fatal = self._fatal or exc

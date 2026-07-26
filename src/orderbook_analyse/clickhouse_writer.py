"""Buffered ClickHouse writer with async queue and non-blocking inserts."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)

SequenceRow = tuple[Any, ...]

TABLE_COLUMNS: dict[str, list[str]] = {
    "orderbook_deltas": [
        "exchange_ts",
        "received_ts",
        "symbol",
        "side",
        "price",
        "quantity",
        "message_type",
        "update_id",
        "cross_sequence",
        "level_index",
    ],
    "public_trades": [
        "trade_ts",
        "received_ts",
        "symbol",
        "trade_id",
        "side",
        "price",
        "quantity",
        "notional",
        "tick_direction",
        "is_block_trade",
        "is_rpi_trade",
    ],
    "ticker_samples": [
        "exchange_ts",
        "received_ts",
        "symbol",
        "last_price",
        "mark_price",
        "index_price",
        "best_bid_price",
        "best_ask_price",
        "open_interest",
        "open_interest_value",
        "funding_rate",
        "volume_24h",
        "turnover_24h",
    ],
    "liquidations": [
        "liquidation_ts",
        "received_ts",
        "symbol",
        "side",
        "price",
        "quantity",
        "notional",
    ],
    "recorder_health": [
        "event_ts",
        "symbol",
        "event_type",
        "stream",
        "message",
        "websocket_reconnects",
        "messages_received",
        "rows_inserted",
        "queue_size",
    ],
}


class ClickHouseClient(Protocol):
    def insert(
        self,
        table: str,
        data: list[SequenceRow],
        column_names: list[str],
    ) -> Any: ...

    def close(self) -> None: ...


ClientFactory = Callable[[], ClickHouseClient]


@dataclass
class WriterStats:
    rows_buffered: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    rows_inserted: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    flush_count: int = 0
    insert_error_count: int = 0
    max_queue_size: int = 0
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows_buffered": dict(self.rows_buffered),
            "rows_inserted": dict(self.rows_inserted),
            "flush_count": self.flush_count,
            "insert_error_count": self.insert_error_count,
            "max_queue_size": self.max_queue_size,
            "last_error": self.last_error,
        }


class InsertError(RuntimeError):
    """Raised when a ClickHouse insert fails."""


class AsyncClickHouseWriter:
    """
    Accepts rows via an asyncio.Queue (backpressure, no silent drops).

    Flushes when:
    - a table buffer reaches batch_size
    - flush_interval elapses
    - shutdown()/flush() is requested
    """

    def __init__(
        self,
        *,
        client_factory: ClientFactory,
        batch_size: int = 5000,
        flush_interval_sec: float = 1.0,
        queue_maxsize: int = 10_000,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be > 0")
        if flush_interval_sec <= 0:
            raise ValueError("flush_interval_sec must be > 0")
        self._client_factory = client_factory
        self.batch_size = batch_size
        self.flush_interval_sec = flush_interval_sec
        self._queue: asyncio.Queue[tuple[str, list[SequenceRow]] | None] = asyncio.Queue(
            maxsize=queue_maxsize
        )
        self._buffers: dict[str, list[SequenceRow]] = defaultdict(list)
        self._client: ClickHouseClient | None = None
        self.stats = WriterStats()
        self._worker_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._fatal_error: BaseException | None = None

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    @property
    def total_rows_inserted(self) -> int:
        return int(sum(self.stats.rows_inserted.values()))

    def _ensure_client(self) -> ClickHouseClient:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    async def start(self) -> None:
        if self._worker_task is not None:
            return
        self._stop.clear()
        self._worker_task = asyncio.create_task(self._worker(), name="ch-writer")

    async def enqueue(self, table: str, rows: list[SequenceRow]) -> None:
        if not rows:
            return
        if table not in TABLE_COLUMNS:
            raise ValueError(f"unknown table: {table}")
        if self._fatal_error is not None:
            raise InsertError(f"writer already failed: {self._fatal_error}") from self._fatal_error
        # Blocks when full → backpressure, no silent data loss
        await self._queue.put((table, rows))
        qsize = self._queue.qsize()
        if qsize > self.stats.max_queue_size:
            self.stats.max_queue_size = qsize

    async def flush(self) -> None:
        """Request flush of all buffers and wait until queue drained + buffers empty."""
        await self._queue.put(None)  # sentinel meaning "flush now"
        # Wait until worker processed through current queue depth + sentinel
        while self._worker_task and not self._worker_task.done():
            if self._queue.empty() and not any(self._buffers.values()):
                break
            if self._fatal_error is not None:
                raise InsertError(str(self._fatal_error)) from self._fatal_error
            await asyncio.sleep(0.01)
        if self._fatal_error is not None:
            raise InsertError(str(self._fatal_error)) from self._fatal_error

    async def stop(self) -> None:
        """Flush remaining rows and stop the worker."""
        self._stop.set()
        # Wake worker if waiting
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            await self._queue.put(None)
        if self._worker_task is not None:
            await self._worker_task
            self._worker_task = None
        if self._fatal_error is not None:
            raise InsertError(str(self._fatal_error)) from self._fatal_error

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                logger.exception("error closing ClickHouse client")
            self._client = None

    async def _worker(self) -> None:
        last_flush_mono = time.monotonic()
        try:
            while not self._stop.is_set() or not self._queue.empty() or any(self._buffers.values()):
                forced_flush = False
                remaining = self.flush_interval_sec - (time.monotonic() - last_flush_mono)
                timeout = max(0.001, min(self.flush_interval_sec, remaining))
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    await self._flush_all()
                    last_flush_mono = time.monotonic()
                    if self._stop.is_set() and self._queue.empty() and not any(
                        self._buffers.values()
                    ):
                        break
                    continue

                if item is None:
                    forced_flush = True
                else:
                    table, rows = item
                    self._buffers[table].extend(rows)
                    self.stats.rows_buffered[table] += len(rows)
                    if len(self._buffers[table]) >= self.batch_size:
                        await self._flush_table(table)
                        last_flush_mono = time.monotonic()

                if forced_flush or (time.monotonic() - last_flush_mono) >= self.flush_interval_sec:
                    await self._flush_all()
                    last_flush_mono = time.monotonic()

                if self._stop.is_set() and self._queue.empty():
                    await self._flush_all()
                    last_flush_mono = time.monotonic()
                    break
        except Exception as exc:  # noqa: BLE001
            self._fatal_error = exc
            self.stats.insert_error_count += 1
            self.stats.last_error = str(exc)
            logger.exception("ClickHouse writer worker failed")
        finally:
            try:
                await self._flush_all()
            except Exception as exc:  # noqa: BLE001
                self._fatal_error = self._fatal_error or exc
                self.stats.insert_error_count += 1
                self.stats.last_error = str(exc)
                logger.exception("ClickHouse final flush failed")

    async def _flush_all(self) -> None:
        for table in list(self._buffers.keys()):
            if self._buffers[table]:
                await self._flush_table(table)

    async def _flush_table(self, table: str) -> None:
        rows = self._buffers[table]
        if not rows:
            return
        # Take ownership so concurrent enqueue goes to a fresh buffer
        chunk = rows
        self._buffers[table] = []
        columns = TABLE_COLUMNS[table]
        try:
            await asyncio.to_thread(self._insert_sync, table, chunk, columns)
        except Exception as exc:
            # Put rows back so stop()/stats can reflect failure; then re-raise
            self._buffers[table] = chunk + self._buffers[table]
            self.stats.insert_error_count += 1
            self.stats.last_error = str(exc)
            logger.exception("ClickHouse insert failed for table=%s rows=%s", table, len(chunk))
            raise InsertError(f"insert failed for {table}: {exc}") from exc

        self.stats.rows_inserted[table] += len(chunk)
        self.stats.flush_count += 1
        logger.debug("flushed %s rows into %s", len(chunk), table)

    def _insert_sync(self, table: str, rows: list[SequenceRow], columns: list[str]) -> None:
        client = self._ensure_client()
        client.insert(table, rows, column_names=columns)


def default_client_factory(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    database: str,
) -> ClientFactory:
    def factory() -> ClickHouseClient:
        import clickhouse_connect

        return clickhouse_connect.get_client(
            host=host,
            port=port,
            username=username,
            password=password,
            database=database,
        )

    return factory

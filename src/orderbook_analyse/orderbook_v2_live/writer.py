"""Bounded async ClickHouse writer for Orderbook V3 live rows.

Defaults (documented):
- queue_capacity=2048: fail-closed; ~40s of 51 rows/s without growing RAM
- insert_batch_size=100: multi-symbol batches, below historical 50k bulk import
- flush_interval_sec=0.5: similar to OI collector 1s flush, slightly tighter
- insert_retry_count=3 with 0.2/0.4/0.8s backoff
- shutdown_flush_timeout_sec=10
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from orderbook_analyse.orderbook_v2.ch_writer import insert_features

logger = logging.getLogger(__name__)


class QueueFullError(RuntimeError):
    """Bounded queue is full. Fail-closed: do not drop the row silently."""


class InsertError(RuntimeError):
    pass


class NullFeatureWriter:
    """No-op writer for raw-archive-only mode (no ClickHouse, no feature queue)."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[list[dict[str, Any]]] = asyncio.Queue(maxsize=1)
        self.queue_capacity = 0
        self.queue_high_watermark = 0
        self.state = "DISABLED"
        self.rows_written = 0
        self.batches_flushed = 0
        self.batch_sizes: list[int] = []
        self.insert_latencies_ms: list[float] = []
        self.insert_failures = 0
        self.last_error = ""

    def enqueue(self, rows: list[dict[str, Any]]) -> None:
        if rows:
            raise RuntimeError("NullFeatureWriter must not receive feature rows")

    def request_stop(self) -> None:
        pass

    async def run(self) -> None:
        while True:
            await asyncio.sleep(3600)

    async def join(self, task: asyncio.Task, timeout: float | None = None) -> bool:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self.state = "STOPPED"
        return True


class FeatureWriter:
    def __init__(
        self,
        client_factory: Callable[[], Any],
        *,
        queue_capacity: int = 2048,
        insert_batch_size: int = 100,
        flush_interval_sec: float = 0.5,
        insert_retry_count: int = 3,
        shutdown_flush_timeout_sec: float = 10.0,
        insert_fn=None,
    ) -> None:
        if queue_capacity < 1:
            raise ValueError("queue_capacity must be >= 1")
        self.queue: asyncio.Queue[list[dict[str, Any]]] = asyncio.Queue(maxsize=queue_capacity)
        self.queue_capacity = queue_capacity
        self.insert_batch_size = insert_batch_size
        self.flush_interval_sec = flush_interval_sec
        self.insert_retry_count = insert_retry_count
        self.shutdown_flush_timeout_sec = shutdown_flush_timeout_sec
        self._client_factory = client_factory
        self._insert_fn = insert_fn or insert_features
        self._client: Any = None
        self._stop = asyncio.Event()
        self.state = "RUNNING"
        self.rows_written = 0
        self.insert_failures = 0
        self.batches_flushed = 0
        self.batch_sizes: list[int] = []
        self.insert_latencies_ms: list[float] = []
        self.last_error = ""
        self.queue_high_watermark = 0
        self.queue_samples: list[int] = []

    def enqueue(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        if self._stop.is_set() and self.state not in {"RUNNING", "FLUSHING"}:
            raise QueueFullError("writer_stopped")
        try:
            self.queue.put_nowait(list(rows))
        except asyncio.QueueFull as exc:
            self.state = "FAIL_CLOSED"
            self.last_error = "queue_full"
            raise QueueFullError("queue_full") from exc
        qsz = self.queue.qsize()
        self.queue_high_watermark = max(self.queue_high_watermark, qsz)
        self.queue_samples.append(qsz)
        if len(self.queue_samples) > 4000:
            self.queue_samples = self.queue_samples[-2000:]

    def request_stop(self) -> None:
        self._stop.set()

    def _flush_batch(self, batch: list[dict[str, Any]]) -> None:
        if not batch:
            return
        if self._client is None:
            self._client = self._client_factory()
        delay = 0.2
        last_exc: Exception | None = None
        attempts = max(1, self.insert_retry_count)
        for attempt in range(attempts):
            t0 = time.monotonic()
            try:
                insert_features_fn = self._insert_fn
                insert_features_fn(self._client, batch)
                latency = (time.monotonic() - t0) * 1000.0
                self.insert_latencies_ms.append(latency)
                if len(self.insert_latencies_ms) > 2000:
                    self.insert_latencies_ms = self.insert_latencies_ms[-1000:]
                self.rows_written += len(batch)
                self.batches_flushed += 1
                self.batch_sizes.append(len(batch))
                if len(self.batch_sizes) > 2000:
                    self.batch_sizes = self.batch_sizes[-1000:]
                return
            except Exception as exc:
                last_exc = exc
                self.insert_failures += 1
                self.last_error = type(exc).__name__
                logger.exception("insert_retry attempt=%s", attempt + 1)
                if attempt + 1 < attempts:
                    time.sleep(delay)
                    delay *= 2
        self.state = "ERROR"
        raise InsertError(str(last_exc) if last_exc else "insert_failed")

    async def run(self) -> None:
        self._client = self._client_factory()
        batch: list[dict[str, Any]] = []
        try:
            while True:
                if self._stop.is_set() and self.queue.empty() and not batch:
                    break
                timeout = 0.05 if self._stop.is_set() else self.flush_interval_sec
                try:
                    item = await asyncio.wait_for(self.queue.get(), timeout=timeout)
                    batch.extend(item)
                    while not self.queue.empty() and len(batch) < self.insert_batch_size:
                        try:
                            batch.extend(self.queue.get_nowait())
                        except asyncio.QueueEmpty:
                            break
                    if len(batch) >= self.insert_batch_size:
                        to_send = batch[: self.insert_batch_size]
                        batch = batch[self.insert_batch_size :]
                        await asyncio.to_thread(self._flush_batch, to_send)
                except asyncio.TimeoutError:
                    if batch:
                        await asyncio.to_thread(self._flush_batch, batch)
                        batch = []
                    elif self._stop.is_set():
                        break
            if batch:
                await asyncio.to_thread(self._flush_batch, batch)
        finally:
            if self.state != "ERROR" and self.state != "FAIL_CLOSED":
                self.state = "STOPPED"

    async def join(self, task: asyncio.Task, timeout: float | None = None) -> bool:
        self.state = "FLUSHING"
        self._stop.set()
        limit = self.shutdown_flush_timeout_sec if timeout is None else timeout
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=limit)
            if self.state != "ERROR":
                self.state = "STOPPED"
            return True
        except asyncio.TimeoutError:
            self.state = "ERROR"
            self.last_error = "shutdown_flush_timeout"
            logger.error("shutdown_flush_timeout queue=%s", self.queue.qsize())
            return False

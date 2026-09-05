"""Bounded non-blocking sink: batch drain, never block the asyncio/book path on disk."""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable

from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.event_writer import (
    ActiveEventWriter,
)
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.record_envelope import (
    approx_envelope_bytes,
    level_update_count,
)

logger = logging.getLogger(__name__)


class QueueFullError(RuntimeError):
    pass


class NonBlockingDeltaSink:
    """
    Enqueue one compact envelope per Bybit WS delta.

    A daemon writer thread batch-drains the queue and performs orjson + zstd + disk I/O
    off the book lock / asyncio loop.
    """

    def __init__(
        self,
        writer: ActiveEventWriter,
        *,
        queue_size: int,
        batch_max_messages: int = 64,
        batch_max_bytes: int = 256 * 1024,
        flush_interval_sec: float = 1.0,
    ) -> None:
        self.writer = writer
        self.queue_size = int(queue_size)
        self.batch_max_messages = max(1, int(batch_max_messages))
        self.batch_max_bytes = max(1024, int(batch_max_bytes))
        self.flush_interval_sec = float(flush_interval_sec)
        self._q: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=self.queue_size)
        self.drops = 0
        self.enqueued = 0
        self.written = 0
        self.written_levels = 0
        self.enqueued_levels = 0
        self.dropped_levels = 0
        self.dropped_bytes_estimate = 0
        self.bytes_written = 0
        self.high_watermark = 0
        self.flush_count = 0
        self.error_count = 0
        self.last_batch_size = 0
        self.ingress_messages = 0
        self._oldest_enqueued_ns: int | None = None
        self._bytes_in_queue = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._pending_rotate: Callable[[], None] | None = None
        self._rotate_done = threading.Event()
        self._last_error: str | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"fr-delta-writer-{writer.symbol}",
            daemon=True,
        )
        self._thread.start()

    @property
    def writer_alive(self) -> bool:
        return self._thread.is_alive() and not self._stop.is_set()

    @property
    def dropped_price_level_updates(self) -> int:
        return int(self.dropped_levels)

    @property
    def backlog(self) -> int:
        return int(self._q.qsize())

    @property
    def backlog_bytes(self) -> int:
        with self._lock:
            return int(self._bytes_in_queue)

    @property
    def oldest_age_ms(self) -> float | None:
        with self._lock:
            if self._oldest_enqueued_ns is None:
                return None
            return max(0.0, (time.time_ns() - self._oldest_enqueued_ns) / 1e6)

    def try_put(self, record: dict[str, Any]) -> bool:
        self.ingress_messages += 1
        levels = int(record.get("level_update_count") or level_update_count(record))
        est = int(record.get("_approx_bytes") or approx_envelope_bytes(record))
        if self._stop.is_set():
            self.drops += 1
            self.dropped_levels += levels
            self.dropped_bytes_estimate += est
            return False
        try:
            self._q.put_nowait(record)
        except queue.Full:
            self.drops += 1
            self.dropped_levels += levels
            self.dropped_bytes_estimate += est
            return False
        with self._lock:
            self.enqueued += 1
            self.enqueued_levels += levels
            self._bytes_in_queue += est
            if self._oldest_enqueued_ns is None:
                self._oldest_enqueued_ns = int(record.get("local_receive_time_ns") or time.time_ns())
            hw = self._q.qsize()
            if hw > self.high_watermark:
                self.high_watermark = hw
        return True

    def _note_dequeued(self, batch: list[dict[str, Any]]) -> None:
        with self._lock:
            for rec in batch:
                est = int(rec.get("_approx_bytes") or approx_envelope_bytes(rec))
                self._bytes_in_queue = max(0, self._bytes_in_queue - est)
            if self._q.empty():
                self._oldest_enqueued_ns = None
            else:
                # Approximate: age from wall clock if queue still non-empty.
                self._oldest_enqueued_ns = time.time_ns()

    def _drain_batch(self, first: dict[str, Any]) -> list[dict[str, Any]]:
        batch = [first]
        batch_bytes = int(first.get("_approx_bytes") or approx_envelope_bytes(first))
        while len(batch) < self.batch_max_messages and batch_bytes < self.batch_max_bytes:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item is None:
                # Preserve sentinel for stop.
                self._q.put(None)
                break
            batch.append(item)
            batch_bytes += int(item.get("_approx_bytes") or approx_envelope_bytes(item))
        return batch

    def _run(self) -> None:
        last_flush = time.monotonic()
        while True:
            try:
                item = self._q.get(timeout=0.05)
            except queue.Empty:
                if self._stop.is_set() and self._q.empty():
                    break
                self._maybe_run_pending_rotate()
                if time.monotonic() - last_flush >= self.flush_interval_sec:
                    try:
                        self.writer.flush_pending()
                        self.flush_count += 1
                        last_flush = time.monotonic()
                    except Exception:
                        self.error_count += 1
                        self.writer.mark_incomplete("writer_flush_error")
                continue
            if item is None:
                break
            batch = self._drain_batch(item)
            self._note_dequeued(batch)
            self.last_batch_size = len(batch)
            try:
                n_bytes, n_levels = self.writer.append_delta_batch(batch)
                self.written += len(batch)
                self.written_levels += n_levels
                self.bytes_written += n_bytes
            except Exception as exc:
                self.drops += len(batch)
                self.dropped_levels += sum(
                    int(r.get("level_update_count") or level_update_count(r)) for r in batch
                )
                self.error_count += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
                self.writer.queue_drops += len(batch)
                self.writer.mark_incomplete("writer_thread_error")
                logger.exception(
                    "fr_writer_batch_failed symbol=%s cont=%s batch=%s err=%s",
                    self.writer.symbol,
                    self.writer.continuation_index,
                    len(batch),
                    self._last_error,
                )
            self._maybe_run_pending_rotate()
            if time.monotonic() - last_flush >= self.flush_interval_sec:
                try:
                    self.writer.flush_pending()
                    self.flush_count += 1
                    last_flush = time.monotonic()
                except Exception:
                    self.error_count += 1
                    self.writer.mark_incomplete("writer_flush_error")
                    logger.exception("fr_writer_flush_failed symbol=%s", self.writer.symbol)
        try:
            self.writer.flush_pending()
            self.flush_count += 1
        except Exception:
            self.error_count += 1

    def _maybe_run_pending_rotate(self) -> None:
        with self._lock:
            cb = self._pending_rotate
            self._pending_rotate = None
        if cb is not None:
            try:
                cb()
            finally:
                self._rotate_done.set()

    def rotate_writer(
        self,
        build_new: Callable[[ActiveEventWriter], tuple[ActiveEventWriter, dict[str, Any]]],
        *,
        timeout_sec: float = 60.0,
    ) -> dict[str, Any]:
        """
        Segment rollover on the writer thread only.

        Queue and writer thread stay alive; producer is not interrupted.
        `build_new(old_writer)` must finalize the old segment and return (new_writer, man).
        """
        box: dict[str, Any] = {"man": {}, "error": None}
        self._rotate_done.clear()

        def _cb() -> None:
            try:
                self.writer.flush_pending()
                self.flush_count += 1
                new_writer, man = build_new(self.writer)
                if not new_writer._opened:
                    new_writer.open()
                self.writer = new_writer
                box["man"] = man
            except Exception as exc:
                box["error"] = exc
                self.error_count += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("fr_segment_rotate_failed symbol=%s", getattr(self.writer, "symbol", "?"))

        with self._lock:
            self._pending_rotate = _cb
        if not self._rotate_done.wait(timeout=timeout_sec):
            raise TimeoutError("segment rotate timed out")
        if box["error"] is not None:
            raise box["error"]
        return box["man"]

    def stop(self, *, timeout_sec: float = 15.0) -> bool:
        """Stop writer thread and drain. Returns True if drained before timeout."""
        self._stop.set()
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout=timeout_sec)
        alive = self._thread.is_alive()
        try:
            self.writer.flush_pending()
        except Exception:
            self.error_count += 1
        if alive:
            self.writer.mark_incomplete("INCOMPLETE_WRITER_DRAIN_TIMEOUT")
            return False
        return True

    def metrics(self) -> dict[str, Any]:
        return {
            "queue_capacity": self.queue_size,
            "queue_backlog_items": self.backlog,
            "queue_backlog_bytes": self.backlog_bytes,
            "queue_oldest_age_ms": self.oldest_age_ms,
            "queue_high_watermark": self.high_watermark,
            "queue_drop_count": self.drops,
            "dropped_messages": self.drops,
            "dropped_price_level_updates": self.dropped_levels,
            "dropped_bytes_estimate": self.dropped_bytes_estimate,
            "enqueued_messages": self.enqueued,
            "enqueued_level_updates": self.enqueued_levels,
            "writer_messages": self.written,
            "writer_level_updates": self.written_levels,
            "writer_bytes": self.bytes_written,
            "writer_batch_size": self.last_batch_size,
            "writer_flush_count": self.flush_count,
            "writer_error_count": self.error_count,
            "writer_alive": self.writer_alive,
            "writer_last_error": self._last_error,
            "ingress_messages": self.ingress_messages,
            "writer_mode": "BATCH_STREAMING_ZSTD",
            "queue_item_contract": "ONE_ITEM_PER_BYBIT_DELTA",
            "continuation_index": self.writer.continuation_index,
        }

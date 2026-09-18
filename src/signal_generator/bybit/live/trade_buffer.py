"""Bounded async queue + durable overflow spool + batch insert for live public trades."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from signal_generator.bybit.live.public_trade_spool import (
    PublicTradeSpool,
    PublicTradeSpoolCorruptError,
    PublicTradeSpoolFullError,
)
from signal_generator.bybit.live.ws_public_trade import WsPublicTrade, ws_trade_to_parsed
from signal_generator.db.public_trades import CanonicalPublicTradeRepository

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_MAXSIZE = 100_000
DEFAULT_BATCH_SIZE = 2_000
DEFAULT_FLUSH_INTERVAL_S = 0.25
DEFAULT_OVERFLOW_BATCH = 200
INSERT_MAX_ATTEMPTS = 8


@dataclass
class PublicTradeHealthMetrics:
    last_trade_event_ts: datetime | None = None
    last_trade_ingest_ts: datetime | None = None
    lag_seconds: float | None = None
    rows_received: int = 0
    rows_inserted: int = 0
    duplicate_rows_skipped: int = 0
    reconnect_count: int = 0
    queue_depth: int = 0
    queue_maxsize: int = 0
    queue_high_watermark: int = 0
    dropped_events: int = 0  # lifetime; must stay 0 after spool fix
    insert_failures: int = 0
    gap_detected: int = 0
    gap_recovered: int = 0
    spool_batches_written: int = 0
    spool_batches_replayed: int = 0
    spool_bytes_used: int = 0
    writer_alive: bool = False
    writer_fatal: bool = False
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_trade_event_ts": (
                self.last_trade_event_ts.isoformat() if self.last_trade_event_ts else None
            ),
            "last_trade_ingest_ts": (
                self.last_trade_ingest_ts.isoformat() if self.last_trade_ingest_ts else None
            ),
            "lag_seconds": self.lag_seconds,
            "rows_received": self.rows_received,
            "rows_inserted": self.rows_inserted,
            "duplicate_rows_skipped": self.duplicate_rows_skipped,
            "reconnect_count": self.reconnect_count,
            "queue_depth": self.queue_depth,
            "queue_maxsize": self.queue_maxsize,
            "queue_high_watermark": self.queue_high_watermark,
            "dropped_events": self.dropped_events,
            "insert_failures": self.insert_failures,
            "gap_detected": self.gap_detected,
            "gap_recovered": self.gap_recovered,
            "spool_batches_written": self.spool_batches_written,
            "spool_batches_replayed": self.spool_batches_replayed,
            "spool_bytes_used": self.spool_bytes_used,
            "writer_alive": self.writer_alive,
            "writer_fatal": self.writer_fatal,
            "last_error": self.last_error,
        }


class PublicTradeInsertBuffer:
    """Non-blocking enqueue with durable overflow; background worker batch-inserts."""

    def __init__(
        self,
        repo: CanonicalPublicTradeRepository,
        *,
        queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE,
        batch_size: int = DEFAULT_BATCH_SIZE,
        flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
        spool_dir: Path | None = None,
        overflow_batch_size: int = DEFAULT_OVERFLOW_BATCH,
    ) -> None:
        self.repo = repo
        self.queue_maxsize = max(1, int(queue_maxsize))
        self.batch_size = max(1, int(batch_size))
        self.flush_interval_s = max(0.05, float(flush_interval_s))
        self.overflow_batch_size = max(1, int(overflow_batch_size))
        self._queue: asyncio.Queue[WsPublicTrade | None] = asyncio.Queue(
            maxsize=self.queue_maxsize
        )
        self.metrics = PublicTradeHealthMetrics(queue_maxsize=self.queue_maxsize)
        self._worker: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._overflow: list[WsPublicTrade] = []
        self._overflow_lock = asyncio.Lock()
        self._spool: PublicTradeSpool | None = None
        if spool_dir is not None:
            self._spool = PublicTradeSpool(Path(spool_dir))
            st = self._spool.stats()
            self.metrics.spool_bytes_used = int(st.get("bytes_used") or 0)

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._stop.clear()
            self.metrics.writer_fatal = False
            self.metrics.writer_alive = True
            self._worker = asyncio.create_task(self._run_supervised())

    async def stop(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        if self._worker is not None:
            try:
                await self._worker
            except Exception:  # noqa: BLE001
                logger.exception("public trade writer stop awaited fatal")
            self._worker = None
        self.metrics.writer_alive = False
        if self._spool is not None:
            # flush pending overflow into spool before close
            if self._overflow:
                try:
                    self._spool.append_trades(list(self._overflow))
                    self.metrics.spool_batches_written += 1
                    self._overflow.clear()
                except Exception:  # noqa: BLE001
                    logger.exception("overflow flush to spool on stop failed")
            self._spool.close()

    def note_reconnect(self) -> None:
        self.metrics.reconnect_count += 1

    def _note_depth(self) -> None:
        depth = self._queue.qsize() + len(self._overflow)
        self.metrics.queue_depth = depth
        if depth > self.metrics.queue_high_watermark:
            self.metrics.queue_high_watermark = depth

    async def _spill_overflow_locked(self) -> None:
        """Caller must hold _overflow_lock. Spill overflow list to durable spool."""
        if not self._overflow:
            return
        if self._spool is None:
            # No spool configured: fail closed rather than silent drop.
            self.metrics.dropped_events += len(self._overflow)
            self.metrics.last_error = "queue_full_no_spool_fail_closed"
            self.metrics.writer_fatal = True
            raise RuntimeError("public_trade_queue_full_no_spool")
        batch = list(self._overflow)
        self._overflow.clear()
        try:
            await asyncio.to_thread(self._spool.append_trades, batch)
            self.metrics.spool_batches_written += 1
            st = self._spool.stats()
            self.metrics.spool_bytes_used = int(st.get("bytes_used") or 0)
            self.metrics.last_error = None
        except PublicTradeSpoolFullError as exc:
            # Put back and fail closed — never silently discard.
            self._overflow = batch + self._overflow
            self.metrics.last_error = f"spool_full:{exc}"
            self.metrics.writer_fatal = True
            raise
        except PublicTradeSpoolCorruptError as exc:
            self._overflow = batch + self._overflow
            self.metrics.last_error = f"spool_corrupt:{exc}"
            self.metrics.writer_fatal = True
            raise

    def enqueue(self, trade: WsPublicTrade) -> bool:
        """Try enqueue without blocking. Spill overflow to spool; never silent-drop."""
        if self.metrics.writer_fatal:
            self.metrics.last_error = "writer_fatal_rejecting_enqueue"
            return False
        self.metrics.rows_received += 1
        self.metrics.last_trade_event_ts = trade.trade_ts
        now = datetime.now(timezone.utc)
        self.metrics.lag_seconds = max(0.0, (now - trade.trade_ts).total_seconds())
        try:
            self._queue.put_nowait(trade)
            self._note_depth()
            return True
        except asyncio.QueueFull:
            # Synchronous spill path: accumulate then write batch via thread later.
            # Use overflow list; schedule spill if large.
            self._overflow.append(trade)
            self._note_depth()
            if len(self._overflow) >= self.overflow_batch_size:
                # Best-effort immediate spill (sync) to keep memory bounded.
                if self._spool is None:
                    self.metrics.last_error = "queue_full_no_spool_fail_closed"
                    self.metrics.writer_fatal = True
                    logger.error("PUBLIC_TRADE_FAIL_CLOSED queue_full_no_spool depth=%s", self._queue.qsize())
                    return False
                batch = self._overflow[: self.overflow_batch_size]
                del self._overflow[: self.overflow_batch_size]
                try:
                    self._spool.append_trades(batch)
                    self.metrics.spool_batches_written += 1
                    st = self._spool.stats()
                    self.metrics.spool_bytes_used = int(st.get("bytes_used") or 0)
                    self.metrics.last_error = "spooled_overflow_batch"
                    self._note_depth()
                    return True
                except Exception as exc:  # noqa: BLE001
                    # Restore and fail closed — never silently discard.
                    self._overflow = batch + self._overflow
                    self.metrics.last_error = f"spool_append_failed:{exc}"[:500]
                    self.metrics.writer_fatal = True
                    logger.exception("PUBLIC_TRADE_SPOOL_FAIL_CLOSED: %s", exc)
                    return False
            self.metrics.last_error = "queue_full_buffered_overflow"
            return True

    async def _run_supervised(self) -> None:
        self.metrics.writer_alive = True
        try:
            await self._run()
        except Exception as exc:  # noqa: BLE001
            self.metrics.writer_fatal = True
            self.metrics.writer_alive = False
            self.metrics.last_error = f"writer_fatal:{exc}"[:500]
            logger.exception("PUBLIC_TRADE_WRITER_FATAL: %s", exc)
            raise
        finally:
            self.metrics.writer_alive = False

    async def _run(self) -> None:
        # Replay any unacked spool from prior process first.
        await self._replay_spool()
        batch: list[WsPublicTrade] = []
        while not self._stop.is_set() or not self._queue.empty() or self._overflow or batch:
            # Opportunistically spill overflow under lock.
            async with self._overflow_lock:
                if len(self._overflow) >= self.overflow_batch_size:
                    await self._spill_overflow_locked()
            try:
                item = await asyncio.wait_for(
                    self._queue.get(), timeout=self.flush_interval_s
                )
            except asyncio.TimeoutError:
                item = None
            if item is None:
                if batch:
                    await self._flush_with_retry(batch)
                    batch = []
                # drain a bit of overflow into batch
                async with self._overflow_lock:
                    if self._overflow and len(batch) < self.batch_size:
                        take = min(self.batch_size - len(batch), len(self._overflow))
                        batch.extend(self._overflow[:take])
                        del self._overflow[:take]
                        self._note_depth()
                if batch and (self._stop.is_set() or len(batch) >= self.batch_size):
                    await self._flush_with_retry(batch)
                    batch = []
                if self._stop.is_set() and self._queue.empty() and not self._overflow and not batch:
                    # final spool replay
                    await self._replay_spool()
                    break
                # periodic spool drain while idle
                if not self._stop.is_set():
                    await self._replay_spool(max_batches=2)
                continue
            batch.append(item)
            self._note_depth()
            if len(batch) >= self.batch_size:
                await self._flush_with_retry(batch)
                batch = []

    async def _replay_spool(self, max_batches: int | None = None) -> None:
        if self._spool is None:
            return
        try:
            batches = list(self._spool.iter_unacked())
        except PublicTradeSpoolCorruptError as exc:
            self.metrics.writer_fatal = True
            self.metrics.last_error = f"spool_corrupt:{exc}"
            raise
        n = 0
        for sp in batches:
            await self._flush_with_retry(sp.trades, spool_seq=sp.seq)
            self.metrics.spool_batches_replayed += 1
            n += 1
            if max_batches is not None and n >= max_batches:
                break

    async def _flush_with_retry(
        self,
        trades: list[WsPublicTrade],
        *,
        spool_seq: int | None = None,
    ) -> None:
        if not trades:
            return
        delay = 0.05
        last_exc: Exception | None = None
        for attempt in range(1, INSERT_MAX_ATTEMPTS + 1):
            try:
                await self._flush(trades)
                if spool_seq is not None and self._spool is not None:
                    await asyncio.to_thread(self._spool.ack, spool_seq)
                return
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                self.metrics.insert_failures += 1
                self.metrics.last_error = str(exc)[:500]
                logger.exception(
                    "public trade insert failed attempt=%s/%s: %s",
                    attempt,
                    INSERT_MAX_ATTEMPTS,
                    exc,
                )
                await asyncio.sleep(delay)
                delay = min(2.0, delay * 2)
        # Exhausted retries: durable-spill then fail closed so main process dies.
        if spool_seq is None:
            if self._spool is not None:
                try:
                    await asyncio.to_thread(self._spool.append_trades, trades)
                    self.metrics.spool_batches_written += 1
                except Exception as spill_exc:  # noqa: BLE001
                    logger.exception("failed spilling after insert retries: %s", spill_exc)
            else:
                self.metrics.dropped_events += len(trades)
        self.metrics.writer_fatal = True
        raise RuntimeError(
            f"public_trade_insert_exhausted retries last={last_exc!r}"
        )

    async def _flush(self, trades: list[WsPublicTrade]) -> None:
        if not trades:
            return
        ingest_ts = datetime.now(timezone.utc)
        parsed = [ws_trade_to_parsed(t) for t in trades]

        def _insert() -> tuple[int, int]:
            # Live path: direct insert. Canonical table is ReplacingMergeTree on
            # (symbol, trade_id) — skip_existing SELECTs cannot keep up with 51-coin
            # WS volume under CH load and only add session/query contention.
            n = self.repo.insert_trades(
                parsed, ingest_timestamp=ingest_ts, source="live"
            )
            return n, 0

        inserted, skipped = await asyncio.to_thread(_insert)
        self.metrics.rows_inserted += inserted
        self.metrics.duplicate_rows_skipped += skipped
        self.metrics.last_trade_ingest_ts = ingest_ts
        self._note_depth()

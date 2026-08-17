"""Bounded async queue + batch insert for live public trades."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from signal_generator.bybit.live.ws_public_trade import WsPublicTrade, ws_trade_to_parsed
from signal_generator.db.public_trades import CanonicalPublicTradeRepository

logger = logging.getLogger(__name__)


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
    dropped_events: int = 0
    insert_failures: int = 0
    gap_detected: int = 0
    gap_recovered: int = 0
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
            "dropped_events": self.dropped_events,
            "insert_failures": self.insert_failures,
            "gap_detected": self.gap_detected,
            "gap_recovered": self.gap_recovered,
            "last_error": self.last_error,
        }


class PublicTradeInsertBuffer:
    """Non-blocking enqueue; background worker batch-inserts to ClickHouse."""

    def __init__(
        self,
        repo: CanonicalPublicTradeRepository,
        *,
        queue_maxsize: int = 5000,
        batch_size: int = 500,
        flush_interval_s: float = 0.5,
    ) -> None:
        self.repo = repo
        self.queue_maxsize = max(1, queue_maxsize)
        self.batch_size = max(1, batch_size)
        self.flush_interval_s = max(0.05, flush_interval_s)
        self._queue: asyncio.Queue[WsPublicTrade | None] = asyncio.Queue(
            maxsize=self.queue_maxsize
        )
        self.metrics = PublicTradeHealthMetrics(queue_maxsize=self.queue_maxsize)
        self._worker: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._stop.clear()
            self._worker = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        if self._worker is not None:
            await self._worker
            self._worker = None

    def note_reconnect(self) -> None:
        self.metrics.reconnect_count += 1

    def enqueue(self, trade: WsPublicTrade) -> bool:
        """Try enqueue without blocking. Returns False if dropped."""
        self.metrics.rows_received += 1
        self.metrics.last_trade_event_ts = trade.trade_ts
        now = datetime.now(timezone.utc)
        self.metrics.lag_seconds = max(
            0.0, (now - trade.trade_ts).total_seconds()
        )
        try:
            self._queue.put_nowait(trade)
            self.metrics.queue_depth = self._queue.qsize()
            return True
        except asyncio.QueueFull:
            self.metrics.dropped_events += 1
            self.metrics.last_error = "queue_full_dropped_event"
            logger.error(
                "PUBLIC_TRADE_DROPPED symbol=%s trade_id=%s queue_depth=%s",
                trade.symbol,
                trade.trade_id,
                self._queue.qsize(),
            )
            return False

    async def _run(self) -> None:
        batch: list[WsPublicTrade] = []
        while not self._stop.is_set() or not self._queue.empty() or batch:
            try:
                item = await asyncio.wait_for(
                    self._queue.get(), timeout=self.flush_interval_s
                )
            except asyncio.TimeoutError:
                item = None
            if item is None:
                if batch:
                    await self._flush(batch)
                    batch = []
                if self._stop.is_set() and self._queue.empty():
                    break
                continue
            batch.append(item)
            self.metrics.queue_depth = self._queue.qsize()
            if len(batch) >= self.batch_size:
                await self._flush(batch)
                batch = []

    async def _flush(self, trades: list[WsPublicTrade]) -> None:
        if not trades:
            return
        ingest_ts = datetime.now(timezone.utc)
        parsed = [ws_trade_to_parsed(t) for t in trades]

        def _insert() -> tuple[int, int]:
            return self.repo.insert_trades_skip_existing(
                parsed, ingest_timestamp=ingest_ts, source="live"
            )

        try:
            inserted, skipped = await asyncio.to_thread(_insert)
            self.metrics.rows_inserted += inserted
            self.metrics.duplicate_rows_skipped += skipped
            self.metrics.last_trade_ingest_ts = ingest_ts
            self.metrics.queue_depth = self._queue.qsize()
        except Exception as exc:  # noqa: BLE001
            self.metrics.insert_failures += 1
            self.metrics.last_error = str(exc)[:500]
            logger.exception("public trade insert failed: %s", exc)

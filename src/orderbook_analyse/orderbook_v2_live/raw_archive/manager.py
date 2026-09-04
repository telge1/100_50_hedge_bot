"""Async raw archive manager: bounded queue + writer task."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from orderbook_analyse.orderbook_v2.book import BookState
from orderbook_analyse.orderbook_v2_live.raw_archive.config import RawArchiveSettings
from orderbook_analyse.orderbook_v2_live.raw_archive.disk import check_disk
from orderbook_analyse.orderbook_v2_live.raw_archive.events import (
    serialize_lifecycle,
    serialize_market_payload,
    serialize_rotation_checkpoint,
)
from orderbook_analyse.orderbook_v2_live.raw_archive.metrics import RawArchiveMetrics
from orderbook_analyse.orderbook_v2_live.raw_archive.segment import (
    SegmentWriter,
    git_head_short,
    segment_directory,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RawQueueItem:
    symbol: str
    payload: bytes
    kind: str
    sequence: int | None = None
    update_id: int | None = None
    received_at: datetime | None = None


class RawArchiveManager:
    """Non-blocking raw OB200 archival sidecar for the live collector."""

    def __init__(self, settings: RawArchiveSettings, *, depth: int = 200) -> None:
        self.settings = settings
        self.depth = depth
        self.metrics = RawArchiveMetrics()
        self._queue: asyncio.Queue[RawQueueItem | None] = asyncio.Queue(
            maxsize=settings.queue_size
        )
        self._writers: dict[str, SegmentWriter] = {}
        self._writer_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._git_head = git_head_short()
        self._rotation_key: dict[str, str] = {}
        self._disk_free_gb: float | None = None

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    def start(self) -> None:
        if not self.settings.enabled:
            return
        self._writer_task = asyncio.create_task(self._run_writer())

    async def stop(self) -> None:
        if not self.settings.enabled:
            return
        while self._queue.qsize() > 0:
            await asyncio.sleep(0.01)
        self._stop.set()
        while True:
            try:
                self._queue.put_nowait(None)
                break
            except asyncio.QueueFull:
                await asyncio.sleep(0.01)
        if self._writer_task is not None:
            await self._writer_task
        now = datetime.now(timezone.utc)
        for symbol, writer in list(self._writers.items()):
            await self._close_segment(symbol, writer, now, clean=True)
        self._writers.clear()

    async def _get_writer(self, symbol: str, ts: datetime) -> SegmentWriter:
        bucket = self._rotation_bucket(ts)
        if symbol in self._writers and self._rotation_key.get(symbol) == bucket:
            return self._writers[symbol]
        if symbol in self._writers:
            old = self._writers.pop(symbol)
            await self._close_segment(symbol, old, ts, clean=False)
        directory = segment_directory(self.settings.archive_root, symbol, ts)
        writer = SegmentWriter(
            symbol=symbol,
            directory=directory,
            start_utc=ts,
            compression=self.settings.compression,
            compression_level=self.settings.compression_level,
        )
        writer.open()
        self._writers[symbol] = writer
        self._rotation_key[symbol] = bucket
        self.metrics.current_segment = str(writer._open_path)
        return writer

    def _rotation_bucket(self, ts: datetime) -> str:
        ts = ts.astimezone(timezone.utc)
        if self.settings.rotation == "hour":
            return ts.strftime("%Y%m%dT%H")
        return ts.strftime("%Y%m%d")

    async def _close_segment(
        self,
        symbol: str,
        writer: SegmentWriter,
        end_ts: datetime,
        *,
        clean: bool,
    ) -> None:
        try:
            if clean:
                line = serialize_lifecycle(
                    "CLEAN_CLOSE",
                    symbol=symbol,
                    ts=end_ts,
                    received_at=end_ts,
                    depth=self.depth,
                )
                writer.write_line(line, kind="marker")
            writer.close(end_utc=end_ts, git_head=self._git_head)
        except Exception as exc:
            self.metrics.writer_errors += 1
            self.metrics.last_error = str(exc)
            logger.exception("raw_archive close failed symbol=%s", symbol)

    def _check_disk(self) -> bool:
        status = check_disk(
            self.settings.archive_root,
            warn_gb=self.settings.warn_free_disk_gb,
            min_gb=self.settings.min_free_disk_gb,
        )
        self._disk_free_gb = status.free_gb
        if status.below_min:
            self.metrics.paused = True
            self.metrics.last_error = "disk_below_min"
            return False
        self.metrics.paused = False
        return True

    def try_enqueue_market(
        self,
        symbol: str,
        payload: dict[str, Any],
        received_at: datetime,
    ) -> None:
        if not self.settings.should_archive(symbol):
            return
        self.metrics.events_received += 1
        if self.metrics.paused or not self._check_disk():
            self._record_overflow(symbol, reason="disk_paused")
            return
        msg_type = str(payload.get("type") or "")
        data = payload.get("data") or {}
        seq = data.get("seq")
        sequence = int(seq) if seq is not None else None
        u_raw = data.get("u")
        update_id = int(u_raw) if u_raw is not None else None
        if msg_type == "snapshot":
            self.metrics.snapshots += 1
            self.metrics.native_snapshots += 1
        elif msg_type == "delta":
            self.metrics.deltas += 1
        if sequence is not None:
            self.metrics.note_sequence(sequence)
        line = serialize_market_payload(payload, received_at=received_at, depth=self.depth)
        item = RawQueueItem(
            symbol=symbol,
            payload=line,
            kind=msg_type or "marker",
            sequence=sequence,
            update_id=update_id,
            received_at=received_at,
        )
        try:
            self._queue.put_nowait(item)
            qsize = self._queue.qsize()
            self.metrics.queue_high_watermark = max(self.metrics.queue_high_watermark, qsize)
        except asyncio.QueueFull:
            self._record_overflow(symbol, reason="queue_full")

    def try_enqueue_checkpoint(
        self,
        symbol: str,
        book: BookState,
        *,
        ts_ms: int,
        received_at: datetime,
        topic: str,
    ) -> None:
        if not self.settings.should_archive(symbol) or not book.is_valid:
            return
        line = serialize_rotation_checkpoint(
            book,
            symbol,
            topic=topic,
            ts_ms=ts_ms,
            received_at=received_at,
            depth=self.depth,
        )
        item = RawQueueItem(
            symbol=symbol,
            payload=line,
            kind="rotation_checkpoint",
            sequence=book.last_seq,
            update_id=book.last_u,
            received_at=received_at,
        )
        try:
            self._queue.put_nowait(item)
            self.metrics.checkpoint_count += 1
        except asyncio.QueueFull:
            self._record_overflow(symbol, reason="queue_full")

    def note_lifecycle(
        self,
        event_type: str,
        *,
        symbol: str | None = None,
        received_at: datetime | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if not self.settings.enabled:
            return
        if symbol is None:
            for sym in sorted(self.settings.symbols):
                self.note_lifecycle(
                    event_type,
                    symbol=sym,
                    received_at=received_at,
                    details=details,
                )
            return
        if not self.settings.should_archive(symbol):
            return
        now = received_at or datetime.now(timezone.utc)
        line = serialize_lifecycle(
            event_type,
            symbol=symbol,
            received_at=now,
            depth=self.depth,
            details=details,
        )
        sym = symbol or "__global__"
        item = RawQueueItem(symbol=sym, payload=line, kind="marker", received_at=now)
        try:
            self._queue.put_nowait(item)
            self.metrics.marker_count += 1
            if event_type in {"SEQUENCE_GAP", "QUEUE_OVERFLOW"}:
                self.metrics.gap_count += 1
            if event_type == "QUEUE_OVERFLOW":
                self.metrics.overflow_count += 1
        except asyncio.QueueFull:
            self.metrics.events_dropped_overflow += 1
            self.metrics.overflow_count += 1

    def note_sequence_gap(self, symbol: str, details: dict[str, Any] | None = None) -> None:
        self.metrics.gap_count += 1
        self.metrics.segment_replayable = False
        if symbol in self._writers:
            self._writers[symbol].mark_non_replayable("sequence_gap")
        self.note_lifecycle("SEQUENCE_GAP", symbol=symbol, details=details)

    def _record_overflow(self, symbol: str, *, reason: str) -> None:
        self.metrics.events_dropped_overflow += 1
        self.metrics.overflow_count += 1
        self.metrics.segment_replayable = False
        if symbol in self._writers:
            self._writers[symbol].stats.queue_overflow += 1
            self._writers[symbol].mark_non_replayable("queue_overflow")
        self.note_lifecycle("QUEUE_OVERFLOW", symbol=symbol, details={"reason": reason})

    async def _run_writer(self) -> None:
        while not self._stop.is_set():
            item = await self._queue.get()
            if item is None:
                break
            try:
                if not self._check_disk():
                    continue
                ts = item.received_at or datetime.now(timezone.utc)
                if item.symbol == "__global__":
                    continue
                writer = await self._get_writer(item.symbol, ts)
                writer.write_line(
                    item.payload,
                    kind=item.kind,
                    sequence=item.sequence,
                    update_id=item.update_id,
                )
                self.metrics.events_written += 1
                self.metrics.bytes_written = sum(
                    w.stats.compressed_bytes for w in self._writers.values()
                )
                self.metrics.uncompressed_bytes += len(item.payload)
                self.metrics.last_write_at = datetime.now(timezone.utc)
                self.metrics.current_segment = str(writer._open_path)
                self.metrics.segment_replayable = writer.stats.replayable
            except Exception as exc:
                self.metrics.writer_errors += 1
                self.metrics.last_error = str(exc)
                self.note_lifecycle("WRITER_ERROR", details={"error": str(exc)})
                logger.exception("raw_archive writer error")

    def health_dict(self) -> dict[str, Any]:
        payload = self.metrics.to_health(
            enabled=self.settings.enabled,
            symbols=sorted(self.settings.symbols),
            queue_size=self.settings.queue_size,
            current_qsize=self._queue.qsize(),
        )
        payload["raw_free_disk_gb"] = self._disk_free_gb
        return payload

    async def rotate_with_checkpoint(
        self,
        symbol: str,
        book: BookState,
        *,
        ts_ms: int,
        received_at: datetime,
        topic: str,
    ) -> None:
        """Start a new replayable segment from local book state."""
        if not self.settings.should_archive(symbol) or not book.is_valid:
            return
        while self._queue.qsize() > 0:
            await asyncio.sleep(0.01)
        if symbol in self._writers:
            old = self._writers.pop(symbol)
            await self._close_segment(symbol, old, received_at, clean=False)
        self._rotation_key[symbol] = ""
        writer = await self._get_writer(symbol, received_at)
        line = serialize_rotation_checkpoint(
            book,
            symbol,
            topic=topic,
            ts_ms=ts_ms,
            received_at=received_at,
            depth=self.depth,
        )
        writer.write_line(line, kind="rotation_checkpoint", sequence=book.last_seq)
        self.metrics.checkpoint_count += 1

"""Fail-fast continuous Full-OB archive manager."""

from __future__ import annotations

import logging
import os
import queue as std_queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from orderbook_analyse.orderbook_v2_live.full_book_state import ConsistentBookSnapshot

from .checkpoint import build_checkpoint_record
from .config import ARCHIVE_FATAL_EXIT_CODE, FullObContinuousRawArchiveSettings
from .disk_safety import DiskSafetyError, DiskState, classify_os_error, require_not_red
from .envelope import build_envelope, build_marker_envelope, new_archive_instance_id
from .metrics import ArchiveMetrics
from .queue import ArchiveQueueItem, ArchiveQueueOverflow, FatalBoundedQueue
from .segment import COMPLETE, FAILED_DISK, GAP, PARTIAL, SegmentWriter, utc_hour
from .tmp_recovery import scan_orphan_tmps

logger = logging.getLogger(__name__)


class FullObContinuousRawArchive:
    def __init__(
        self,
        settings: FullObContinuousRawArchiveSettings,
        *,
        collector_instance_id: str | None = None,
        snapshot_provider: Callable[[str], ConsistentBookSnapshot | None] | None = None,
        fatal_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.archive_instance_id = new_archive_instance_id()
        self.collector_instance_id = collector_instance_id or f"collector-{os.getpid()}"
        self.snapshot_provider = snapshot_provider
        self.fatal_callback = fatal_callback
        self.metrics = ArchiveMetrics()
        self._queue = FatalBoundedQueue(settings.queue_size)
        self._writers: dict[str, SegmentWriter] = {}
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._fatal_reason: str | None = None
        self._fatal_lock = threading.Lock()
        self._last_checkpoint_bucket: dict[str, int] = {}
        self._disk_state: DiskState | None = None
        self._disk_free_gb: float | None = None
        self._disk_free_percent: float | None = None
        self._orphan_count = 0
        self._attached = False
        self._full_book: Any = None

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    @property
    def fatal_state(self) -> str | None:
        return self._fatal_reason

    def attach(self, full_book_manager: Any) -> None:
        self._full_book = full_book_manager
        if not self.enabled or full_book_manager is None or self._attached:
            return
        full_book_manager.add_observer(self.on_full_ob_message)
        if self.snapshot_provider is None:
            def provider(symbol: str) -> ConsistentBookSnapshot | None:
                runtime = full_book_manager.runtimes.get(symbol.upper())
                if runtime is None:
                    return None
                with full_book_manager._book_lock:
                    snap = runtime.book.copy_consistent_snapshot()
                return snap if snap.book_ready else None

            self.snapshot_provider = provider
        self._attached = True

    def ensure_keeper_leases(self) -> None:
        if not self.enabled or self._full_book is None:
            return
        for symbol in self.settings.symbols:
            try:
                self._full_book._acquire(
                    symbol=symbol, lease_id=f"full-ob-continuous-archive:{symbol}"
                )
            except Exception as exc:
                self._set_fatal(f"keeper_lease:{symbol}:{type(exc).__name__}")
                return

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._orphan_count = len(scan_orphan_tmps(self.settings.archive_root))
        try:
            self._check_disk()
        except Exception as exc:
            reason = classify_os_error(exc) or str(exc) or type(exc).__name__
            self._set_fatal(f"disk_fatal:{reason}")
            return
        self._thread = threading.Thread(target=self._run, name="full-ob-archive", daemon=True)
        self._thread.start()

    def stop(self, *, clean: bool = True) -> None:
        if not self.enabled or self._thread is None:
            return
        fatal = self.fatal_state is not None
        if clean and not fatal:
            for symbol in sorted(self.settings.symbols):
                snap = self._snapshot(symbol)
                if snap is not None:
                    self.enqueue_checkpoint(symbol, snap, reason="shutdown")
        if not fatal:
            try:
                self._queue.stop()
            except ArchiveQueueOverflow:
                self._set_fatal("shutdown_queue_timeout")
        self._thread.join(timeout=30.0)
        if self._thread.is_alive():
            self._set_fatal("writer_shutdown_timeout")
        self._thread = None

    def _snapshot(self, symbol: str) -> ConsistentBookSnapshot | None:
        if self.snapshot_provider is None:
            return None
        try:
            return self.snapshot_provider(symbol)
        except Exception as exc:
            logger.exception("full_ob_archive_snapshot_failed symbol=%s", symbol)
            self._set_fatal(f"snapshot_provider:{type(exc).__name__}")
            return None

    def _put(self, symbol: str, record: dict[str, Any]) -> None:
        if not self.enabled or self.fatal_state is not None:
            return
        try:
            self._queue.put(ArchiveQueueItem(symbol.upper(), record))
            self.metrics.messages_enqueued += 1
        except ArchiveQueueOverflow:
            self.metrics.overflow_count += 1
            writer = self._writers.get(symbol.upper())
            if writer is not None:
                writer.note_overflow()
            self._set_fatal("queue_overflow")

    def enqueue_message(
        self,
        symbol: str,
        payload: dict[str, Any],
        *,
        receive_time_ns: int,
        message_type: str | None = None,
    ) -> None:
        if not self.settings.should_archive(symbol):
            return
        record = build_envelope(
            payload,
            archive_instance_id=self.archive_instance_id,
            collector_instance_id=self.collector_instance_id,
            symbol=symbol,
            receive_time_ns=receive_time_ns,
            message_type=message_type,
        )
        self._put(symbol, record)

    def enqueue_checkpoint(
        self,
        symbol: str,
        snapshot: ConsistentBookSnapshot,
        *,
        reason: str,
    ) -> None:
        if not self.settings.should_archive(symbol):
            return
        try:
            record = build_checkpoint_record(
                snapshot,
                reason=reason,
                archive_instance_id=self.archive_instance_id,
                collector_instance_id=self.collector_instance_id,
            )
        except ValueError:
            return
        self.metrics.checkpoint_count += 1
        self._put(symbol, record)

    def note_gap(
        self,
        symbol: str,
        details: dict[str, Any] | None = None,
        *,
        receive_time_ns: int | None = None,
    ) -> None:
        self.metrics.gap_count += 1
        self._put(
            symbol,
            build_marker_envelope(
                archive_instance_id=self.archive_instance_id,
                collector_instance_id=self.collector_instance_id,
                symbol=symbol,
                message_type="gap_marker",
                details=details or {"reason": "sequence_gap"},
                receive_time_ns=receive_time_ns,
            ),
        )

    def note_reconnect(
        self,
        symbol: str,
        details: dict[str, Any] | None = None,
        *,
        receive_time_ns: int | None = None,
    ) -> None:
        self.metrics.reconnect_count += 1
        body = {"event": "reconnect", **(details or {})}
        self._put(
            symbol,
            build_marker_envelope(
                archive_instance_id=self.archive_instance_id,
                collector_instance_id=self.collector_instance_id,
                symbol=symbol,
                message_type="lifecycle",
                details=body,
                receive_time_ns=receive_time_ns,
            ),
        )

    def on_full_ob_message(
        self,
        *,
        symbol: str,
        payload: dict[str, Any],
        received_at: datetime,
        receive_time_ns: int,
        phase: str,
        outcome: str | None = None,
        runtime: Any = None,
    ) -> None:
        if not self.settings.should_archive(symbol):
            return
        if phase == "reconnect":
            self.note_reconnect(symbol, payload, receive_time_ns=receive_time_ns)
            self.note_gap(
                symbol,
                {"reason": payload.get("reason") or "transport_reconnect"},
                receive_time_ns=receive_time_ns,
            )
            return
        if phase == "resync_ready":
            self.enqueue_message(symbol, payload, receive_time_ns=receive_time_ns, message_type="snapshot")
            snap = self._snapshot(symbol)
            if snap is not None:
                self.enqueue_checkpoint(symbol, snap, reason="reconnect_resync")
            return
        if phase == "buffer" or (phase == "live" and outcome == "applied"):
            self.enqueue_message(symbol, payload, receive_time_ns=receive_time_ns)
        elif outcome in {"gap", "u_reset"}:
            self.note_gap(
                symbol,
                {"reason": outcome, "u": (payload.get("data") or {}).get("u")},
                receive_time_ns=receive_time_ns,
            )

    def tick(self, now: datetime | None = None) -> None:
        if not self.enabled or self.fatal_state is not None:
            return
        self.ensure_keeper_leases()
        value = now or datetime.now(timezone.utc)
        bucket = int(value.timestamp()) // (self.settings.checkpoint_minutes * 60)
        for symbol in sorted(self.settings.symbols):
            if self._last_checkpoint_bucket.get(symbol) == bucket:
                continue
            snap = self._snapshot(symbol)
            if snap is not None:
                self.enqueue_checkpoint(symbol, snap, reason="periodic_5m")
                self._last_checkpoint_bucket[symbol] = bucket

    def _check_disk(self) -> None:
        previous = self._disk_state
        status = require_not_red(
            self.settings.archive_root,
            warn_gb=self.settings.warn_free_disk_gb,
            red_gb=self.settings.red_free_disk_gb,
            warn_percent=self.settings.warn_free_disk_percent,
            red_percent=self.settings.red_free_disk_percent,
        )
        self._disk_state = status.state
        self._disk_free_gb = status.free_gb
        self._disk_free_percent = status.free_percent
        if status.state is DiskState.AMBER and previous is not DiskState.AMBER:
            logger.warning(
                "full_ob_archive_disk_amber free_gb=%.3f free_percent=%.3f",
                status.free_gb,
                status.free_percent,
            )

    def _new_writer(self, symbol: str, ts: datetime) -> SegmentWriter:
        writer = SegmentWriter(
            root=self.settings.archive_root,
            symbol=symbol,
            start_time=ts,
            archive_instance_id=self.archive_instance_id,
            collector_instance_id=self.collector_instance_id,
            compression_level=self.settings.compression_level,
        )
        writer.open()
        snap = self._snapshot(symbol)
        if snap is not None:
            # Pin checkpoint receive time to the segment hour so rotation stays stable
            # even if the live book still carries an older receive timestamp.
            record = build_checkpoint_record(
                snap,
                reason="segment_start",
                archive_instance_id=self.archive_instance_id,
                collector_instance_id=self.collector_instance_id,
                checkpoint_time=ts,
            )
            record["receive_time_ns"] = int(ts.timestamp() * 1_000_000_000)
            writer.write(record)
            self.metrics.checkpoint_count += 1
        self._writers[symbol] = writer
        return writer

    def _writer_for(self, symbol: str, record: dict[str, Any]) -> SegmentWriter:
        payload = record.get("original_payload") if isinstance(record.get("original_payload"), dict) else {}
        # Shutdown and in-segment markers must never open a new wall-clock hour.
        writer = self._writers.get(symbol)
        if writer is not None and (
            record.get("message_type") in {"gap_marker", "lifecycle"}
            or (
                record.get("message_type") == "checkpoint"
                and payload.get("checkpoint_reason") == "shutdown"
            )
        ):
            return writer
        recv_ns = int(record.get("receive_time_ns") or time.time_ns())
        ts = datetime.fromtimestamp(recv_ns / 1e9, tz=timezone.utc)
        if writer is not None and utc_hour(ts) != writer.start_time:
            self._finalize(symbol, writer, clean=True, end_time=ts)
            writer = None
        return writer or self._new_writer(symbol, ts)

    def _finalize(
        self, symbol: str, writer: SegmentWriter, *, clean: bool, end_time: datetime
    ) -> None:
        _, _, manifest = writer.close(end_time=end_time, clean=clean)
        if manifest["completion_status"] == COMPLETE:
            self.metrics.completed_segments += 1
        else:
            self.metrics.partial_segments += 1
        self._writers.pop(symbol, None)

    def _run(self) -> None:
        next_flush = time.monotonic() + self.settings.flush_interval_sec
        try:
            while not self._stopping.is_set():
                timeout = max(0.0, next_flush - time.monotonic())
                try:
                    item = self._queue.get(timeout=timeout)
                except std_queue.Empty:
                    item = ...
                if item is None:
                    self._queue.task_done()
                    break
                if item is not ...:
                    try:
                        self._check_disk()
                        writer = self._writer_for(item.symbol, item.record)
                        writer.write(item.record)
                        self.metrics.messages_written += 1
                        payload = item.record.get("original_payload")
                        data = payload.get("data") if isinstance(payload, dict) else {}
                        if isinstance(data, dict):
                            self.metrics.level_update_count += len(data.get("b") or [])
                            self.metrics.level_update_count += len(data.get("a") or [])
                        self.metrics.uncompressed_bytes += len(str(item.record))
                    finally:
                        self._queue.task_done()
                if time.monotonic() >= next_flush:
                    self._check_disk()
                    for writer in self._writers.values():
                        writer.flush()
                    self.metrics.flush_count += 1
                    next_flush = time.monotonic() + self.settings.flush_interval_sec
        except Exception as exc:
            self.metrics.writer_error_count += 1
            reason = classify_os_error(exc) or str(exc) or type(exc).__name__
            if isinstance(exc, DiskSafetyError) or classify_os_error(exc):
                reason = f"disk_fatal:{reason}"
            self._set_fatal(reason)
        finally:
            now = datetime.now(timezone.utc)
            for symbol, writer in list(self._writers.items()):
                try:
                    requested = FAILED_DISK if str(self.fatal_state or "").startswith("disk_fatal:") else None
                    _, _, manifest = writer.close(
                        end_time=now,
                        clean=self.fatal_state is None,
                        requested_status=requested,
                    )
                    if manifest["completion_status"] == COMPLETE:
                        self.metrics.completed_segments += 1
                    else:
                        self.metrics.partial_segments += 1
                except Exception:
                    logger.exception("full_ob_archive_finalize_failed symbol=%s", symbol)
            self._writers.clear()

    def _set_fatal(self, reason: str) -> None:
        with self._fatal_lock:
            if self._fatal_reason is not None:
                return
            self._fatal_reason = reason
            self._stopping.set()
        logger.error("full_ob_archive_fatal reason=%s exit_code=%s", reason, ARCHIVE_FATAL_EXIT_CODE)
        if self.fatal_callback is not None:
            try:
                self.fatal_callback(reason)
            except Exception:
                logger.exception("full_ob_archive_fatal_callback_failed")

    def health_dict(self) -> dict[str, Any]:
        return {
            "full_ob_raw_archive_enabled": self.enabled,
            "full_ob_raw_archive_instance_id": self.archive_instance_id,
            "full_ob_raw_archive_symbols": sorted(self.settings.symbols),
            "full_ob_raw_archive_queue_depth": self._queue.qsize(),
            "full_ob_raw_archive_queue_capacity": self._queue.capacity,
            "full_ob_raw_archive_queue_high_watermark": self._queue.high_watermark,
            "full_ob_raw_archive_fatal": self.fatal_state is not None,
            "full_ob_raw_archive_fatal_reason": self.fatal_state,
            "full_ob_raw_archive_exit_code": ARCHIVE_FATAL_EXIT_CODE if self.fatal_state else 0,
            "full_ob_raw_archive_disk_state": None if self._disk_state is None else self._disk_state.value,
            "full_ob_raw_archive_disk_free_gb": self._disk_free_gb,
            "full_ob_raw_archive_disk_free_percent": self._disk_free_percent,
            "full_ob_raw_archive_orphan_tmp_count": self._orphan_count,
            "full_ob_raw_archive_flush_interval_sec": self.settings.flush_interval_sec,
            "full_ob_raw_archive_no_silent_discard": True,
            **{f"full_ob_raw_archive_{k}": v for k, v in self.metrics.as_dict().items()},
        }

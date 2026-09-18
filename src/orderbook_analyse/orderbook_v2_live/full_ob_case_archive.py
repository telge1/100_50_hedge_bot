"""Case-scoped Full-OB raw archive using full_ob_continuous_raw_archive_v1 wire format.

Attaches to FullBookOnDemandManager.add_observer. Does NOT replace BTC/DOGE static
keepers; uses per-symbol refcount.

IMPORTANT: Uses continuous Full-OB envelopes (Bronze/Silver v1_3 compatible),
NOT the OB200 raw_archive format.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.orderbook_v2_live.full_book_state import ConsistentBookSnapshot
from orderbook_analyse.orderbook_v2_live.full_ob_continuous_raw_archive.checkpoint import (
    build_checkpoint_record,
)
from orderbook_analyse.orderbook_v2_live.full_ob_continuous_raw_archive.config import (
    FORMAT_VERSION,
)
from orderbook_analyse.orderbook_v2_live.full_ob_continuous_raw_archive.envelope import (
    build_envelope,
    build_marker_envelope,
    new_archive_instance_id,
)
from orderbook_analyse.orderbook_v2_live.full_ob_continuous_raw_archive.segment import (
    COMPLETE,
    GAP,
    PARTIAL,
    SegmentWriter,
)

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE = 16384
DEFAULT_ARCHIVE_ROOT = Path("/tmp/orderbook_full_ob_case_archive_v1")
SOURCE_NAME = "full_ob_case_archive_v1"
ARCHIVE_FORMAT_VERSION = FORMAT_VERSION  # full_ob_continuous_raw_archive_v1
COLLECTOR_INSTANCE_ID = "ema-case-archive-research-v1"


def _utc_iso(dt: datetime | None = None) -> str:
    d = dt or datetime.now(timezone.utc)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class CaseArchiveSession:
    recorder_id: str
    case_id: str
    experiment_id: str
    symbol: str
    archive_duration_seconds: float
    archive_root: Path
    created_at: str
    archive_instance_id: str
    max_queue: int = DEFAULT_QUEUE_SIZE
    queue: deque[dict[str, Any]] = field(default_factory=deque)
    lock: threading.Lock = field(default_factory=threading.Lock)
    writer: SegmentWriter | None = None
    writer_thread: threading.Thread | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    archive_requested: bool = True
    archive_started: bool = False
    archive_ready: bool = False
    coverage_valid: bool = True
    overflow: bool = False
    dropped_count: int = 0
    enqueued_count: int = 0
    written_count: int = 0
    snapshot_written: bool = False
    snapshot_generation: int = 0
    start_sequence: int | None = None
    start_update_id: int | None = None
    archive_path: str | None = None
    manifest_path: str | None = None
    archive_started_at: str | None = None
    finalize_error: str | None = None
    closed: bool = False
    gap_seen: bool = False


@dataclass
class SymbolRefCount:
    count: int = 0
    static_keeper: bool = False
    case_ids: set[str] = field(default_factory=set)


class FullObCaseArchiveHub:
    """Per-case Full-OB segments in continuous archive v1 format (no second Bybit WS)."""

    def __init__(
        self,
        *,
        default_archive_root: Path | None = None,
        default_queue_size: int = DEFAULT_QUEUE_SIZE,
        collector_instance_id: str = COLLECTOR_INSTANCE_ID,
    ) -> None:
        self.default_archive_root = Path(default_archive_root or DEFAULT_ARCHIVE_ROOT)
        self.default_queue_size = int(default_queue_size)
        self.collector_instance_id = collector_instance_id
        self._sessions: dict[str, CaseArchiveSession] = {}
        self._by_symbol: dict[str, set[str]] = {}
        self._refcount: dict[str, SymbolRefCount] = {}
        self._lock = threading.RLock()
        self._attached = False
        self._manager: Any = None
        self._generation: dict[str, int] = {}

    def attach(self, manager: Any) -> None:
        with self._lock:
            if self._attached and self._manager is manager:
                return
            self._manager = manager
            manager.add_observer(self.on_full_ob_message)
            self._attached = True

    def detach(self) -> None:
        with self._lock:
            self._attached = False
            self._manager = None

    def register_static_keeper(self, symbol: str) -> None:
        sym = symbol.upper()
        with self._lock:
            rc = self._refcount.setdefault(sym, SymbolRefCount())
            if not rc.static_keeper:
                rc.static_keeper = True
                rc.count += 1

    def start_case_archive(
        self,
        *,
        symbol: str,
        case_id: str,
        experiment_id: str,
        archive_duration_seconds: float = 300.0,
        archive_root: str | Path | None = None,
        archive_enabled: bool = True,
        recorder_id: str | None = None,
    ) -> dict[str, Any]:
        if not archive_enabled:
            return {"ok": False, "error": "archive_disabled", "archive_requested": False}
        sym = symbol.upper().strip()
        cid = str(case_id or "").strip()
        eid = str(experiment_id or "").strip()
        if not sym or not cid or not eid:
            return {"ok": False, "error": "symbol_case_experiment_required"}
        rid = recorder_id or f"car-{uuid.uuid4().hex[:12]}"
        root = Path(archive_root) if archive_root else self.default_archive_root
        now = datetime.now(timezone.utc)
        session = CaseArchiveSession(
            recorder_id=rid,
            case_id=cid,
            experiment_id=eid,
            symbol=sym,
            archive_duration_seconds=float(archive_duration_seconds),
            archive_root=root,
            created_at=_utc_iso(now),
            archive_instance_id=new_archive_instance_id(),
            max_queue=self.default_queue_size,
        )
        with self._lock:
            if rid in self._sessions:
                return {"ok": False, "error": "recorder_exists", "recorder_id": rid}
            rc = self._refcount.setdefault(sym, SymbolRefCount())
            rc.count += 1
            rc.case_ids.add(cid)
            self._sessions[rid] = session
            self._by_symbol.setdefault(sym, set()).add(rid)

        try:
            self._open_writer(session, now)
            self._maybe_write_initial_checkpoint(session)
            session.archive_started = True
            session.archive_started_at = _utc_iso(now)
            session.archive_ready = bool(session.snapshot_written and session.writer is not None)
        except Exception as exc:
            logger.exception("case_archive_start_failed")
            session.coverage_valid = False
            session.finalize_error = str(exc)
            return {
                "ok": False,
                "error": "archive_start_failed",
                "detail": str(exc),
                "recorder_id": rid,
                "coverage_valid": False,
            }
        return self._status_dict(session, archive_requested=True)

    def _open_writer(self, session: CaseArchiveSession, ts: datetime) -> None:
        # Case isolation under continuous root layout without changing filename contract
        case_root = session.archive_root / f"case_{session.case_id}"
        writer = SegmentWriter(
            root=case_root,
            symbol=session.symbol,
            start_time=ts,
            archive_instance_id=session.archive_instance_id,
            collector_instance_id=self.collector_instance_id,
            compression_level=3,
        )
        writer.open()
        session.writer = writer
        session.stop_event.clear()
        t = threading.Thread(
            target=self._writer_loop,
            args=(session,),
            name=f"case-archive-{session.recorder_id}",
            daemon=True,
        )
        session.writer_thread = t
        t.start()

    def _book_snapshot(self, symbol: str) -> ConsistentBookSnapshot | None:
        mgr = self._manager
        if mgr is None:
            return None
        rt = getattr(mgr, "runtimes", {}).get(symbol)
        if rt is None or not getattr(rt.book, "book_ready", False):
            return None
        book = rt.book
        if hasattr(book, "consistent_snapshot"):
            return book.consistent_snapshot()
        return ConsistentBookSnapshot(
            symbol=symbol,
            bids=dict(book.bids),
            asks=dict(book.asks),
            update_id=book.update_id,
            seq=book.seq,
            event_ts_ms=book.event_ts_ms,
            cts_ms=book.cts_ms,
            receive_time_ns=book.last_receive_time_ns,
            book_ready=book.book_ready,
            last_event_at=book.last_event_at,
        )

    def _maybe_write_initial_checkpoint(self, session: CaseArchiveSession) -> None:
        snap = self._book_snapshot(session.symbol)
        if snap is None:
            return
        try:
            record = build_checkpoint_record(
                snap,
                reason="segment_start",
                archive_instance_id=session.archive_instance_id,
                collector_instance_id=self.collector_instance_id,
                source_snapshot_id=f"case:{session.case_id}",
            )
        except ValueError as exc:
            # Crossed/invalid book: do not crash start; wait for valid resync/snapshot.
            logger.warning("case_archive_initial_checkpoint_skipped: %s", exc)
            return
        record["original_payload"]["case_id"] = session.case_id
        record["original_payload"]["experiment_id"] = session.experiment_id
        record["original_payload"]["recorder_id"] = session.recorder_id
        session.snapshot_generation = max(1, self._generation.get(session.symbol, 1))
        session.start_sequence = snap.seq
        session.start_update_id = snap.update_id
        self._enqueue_record(session, record)
        session.snapshot_written = True
        session.archive_ready = True

    def _enqueue_record(self, session: CaseArchiveSession, record: dict[str, Any]) -> None:
        with session.lock:
            if session.closed:
                return
            if len(session.queue) >= session.max_queue:
                session.overflow = True
                session.coverage_valid = False
                session.dropped_count += 1
                if session.writer is not None:
                    session.writer.note_overflow()
                return
            session.queue.append(record)
            session.enqueued_count += 1

    def _writer_loop(self, session: CaseArchiveSession) -> None:
        while True:
            item = None
            with session.lock:
                if session.queue:
                    item = session.queue.popleft()
                elif session.stop_event.is_set():
                    break
            if item is None:
                time.sleep(0.001)
                continue
            try:
                if session.writer is not None:
                    session.writer.write(item)
                    session.written_count += 1
                    if session.written_count % 64 == 0:
                        session.writer.flush()
            except Exception:
                session.coverage_valid = False
                logger.exception("case_archive_write_failed recorder=%s", session.recorder_id)

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
        **_extra: Any,
    ) -> None:
        if self._manager is not None and not self._attached:
            return
        sym = symbol.upper()
        with self._lock:
            ids = list(self._by_symbol.get(sym, ()))
        if not ids:
            return
        if phase == "resync_ready":
            self._generation[sym] = max(self._generation.get(sym, 0), 1)
        if phase == "live" and outcome in {"gap", "u_reset"}:
            self._generation[sym] = self._generation.get(sym, 0) + 1

        for rid in ids:
            with self._lock:
                session = self._sessions.get(rid)
            if session is None or session.closed:
                continue

            if phase == "resync_ready" and not session.snapshot_written:
                snap = self._book_snapshot(sym)
                if snap is not None:
                    try:
                        record = build_checkpoint_record(
                            snap,
                            reason="reconnect_resync",
                            archive_instance_id=session.archive_instance_id,
                            collector_instance_id=self.collector_instance_id,
                            source_snapshot_id=f"case:{session.case_id}:resync",
                        )
                    except ValueError as exc:
                        logger.warning("case_archive_resync_checkpoint_skipped: %s", exc)
                        continue
                    record["original_payload"]["case_id"] = session.case_id
                    record["original_payload"]["experiment_id"] = session.experiment_id
                    self._enqueue_record(session, record)
                    session.snapshot_written = True
                    session.archive_ready = True
                    session.snapshot_generation = int(self._generation.get(sym, 1))
                    session.start_sequence = snap.seq
                    session.start_update_id = snap.update_id
                continue

            if phase == "live" and outcome in {"gap", "u_reset"}:
                session.coverage_valid = False
                session.gap_seen = True
                marker = build_marker_envelope(
                    archive_instance_id=session.archive_instance_id,
                    collector_instance_id=self.collector_instance_id,
                    symbol=sym,
                    message_type="gap_marker",
                    details={
                        "phase": phase,
                        "outcome": outcome,
                        "case_id": session.case_id,
                        "experiment_id": session.experiment_id,
                    },
                    receive_time_ns=int(receive_time_ns),
                )
                self._enqueue_record(session, marker)
                continue

            msg_type = str(payload.get("type") or "").lower()
            if msg_type not in {"snapshot", "delta"} and phase != "resync_ready":
                continue
            kind = "snapshot" if msg_type == "snapshot" or phase == "resync_ready" else "delta"
            env = build_envelope(
                payload,
                archive_instance_id=session.archive_instance_id,
                collector_instance_id=self.collector_instance_id,
                symbol=sym,
                receive_time_ns=int(receive_time_ns),
                message_type=kind,
            )
            # case identity in original_payload (non-breaking for replay which uses type/data)
            if isinstance(env.get("original_payload"), dict):
                env["original_payload"]["case_id"] = session.case_id
                env["original_payload"]["experiment_id"] = session.experiment_id
                env["original_payload"]["recorder_id"] = session.recorder_id
                env["original_payload"]["snapshot_generation"] = int(
                    self._generation.get(sym, session.snapshot_generation)
                )
            if kind == "snapshot":
                session.snapshot_written = True
                session.archive_ready = True
            self._enqueue_record(session, env)

    def finalize_case_archive(self, recorder_id: str) -> dict[str, Any]:
        rid = str(recorder_id or "").strip()
        with self._lock:
            session = self._sessions.get(rid)
        if session is None:
            return {"ok": False, "error": "unknown_recorder"}
        if session.closed:
            return self._status_dict(session, archive_requested=True)

        deadline = time.time() + 5.0
        while time.time() < deadline:
            with session.lock:
                empty = len(session.queue) == 0
            if empty:
                break
            time.sleep(0.01)
        session.stop_event.set()
        if session.writer_thread is not None:
            session.writer_thread.join(timeout=5.0)

        end = datetime.now(timezone.utc)
        try:
            if session.writer is not None:
                if session.coverage_valid and not session.gap_seen:
                    shutdown_marker = build_marker_envelope(
                        archive_instance_id=session.archive_instance_id,
                        collector_instance_id=self.collector_instance_id,
                        symbol=session.symbol,
                        message_type="lifecycle",
                        details={
                            "event": "CLEAN_CLOSE",
                            "case_id": session.case_id,
                            "experiment_id": session.experiment_id,
                        },
                    )
                    session.writer.write(shutdown_marker)
                status = COMPLETE
                if session.gap_seen or not session.coverage_valid:
                    status = GAP if session.gap_seen else PARTIAL
                if session.overflow or not session.snapshot_written:
                    status = PARTIAL
                final_path, manifest_path, _manifest = session.writer.close(
                    end_time=end,
                    clean=session.coverage_valid and not session.overflow,
                    requested_status=status,
                )
                session.archive_path = str(final_path)
                session.manifest_path = str(manifest_path)
                sidecar = {
                    "archive_format_version": ARCHIVE_FORMAT_VERSION,
                    "recorder_id": session.recorder_id,
                    "case_id": session.case_id,
                    "experiment_id": session.experiment_id,
                    "symbol": session.symbol,
                    "archive_started_at": session.archive_started_at,
                    "archive_ended_at": _utc_iso(end),
                    "snapshot_generation": session.snapshot_generation,
                    "start_sequence": session.start_sequence,
                    "start_update_id": session.start_update_id,
                    "coverage_valid": session.coverage_valid and not session.overflow,
                    "overflow": session.overflow,
                    "snapshot_written": session.snapshot_written,
                    "enqueued_count": session.enqueued_count,
                    "written_count": session.written_count,
                    "dropped_count": session.dropped_count,
                    "segment_path": session.archive_path,
                    "segment_manifest_path": session.manifest_path,
                    "completion_status": status,
                }
                side_path = Path(str(final_path) + ".case.json")
                side_path.write_text(
                    __import__("json").dumps(sidecar, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
        except Exception as exc:
            session.coverage_valid = False
            session.finalize_error = str(exc)
            logger.exception("case_archive_finalize_failed")

        session.closed = True
        self._release_refcount(session)
        with self._lock:
            self._sessions.pop(rid, None)
            bucket = self._by_symbol.get(session.symbol)
            if bucket is not None:
                bucket.discard(rid)
                if not bucket:
                    self._by_symbol.pop(session.symbol, None)

        out = self._status_dict(session, archive_requested=True)
        out["ok"] = session.finalize_error is None and bool(session.archive_path)
        if session.finalize_error:
            out["error"] = "finalize_failed"
            out["detail"] = session.finalize_error
        return out

    def _release_refcount(self, session: CaseArchiveSession) -> None:
        with self._lock:
            rc = self._refcount.get(session.symbol)
            if rc is None:
                return
            rc.case_ids.discard(session.case_id)
            rc.count = max(0, rc.count - 1)

    def release_case_archive(self, recorder_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(recorder_id)
        if session is None:
            return {"ok": True, "removed": False, "recorder_id": recorder_id}
        if not session.closed:
            return self.finalize_case_archive(recorder_id)
        return {"ok": True, "removed": True, "recorder_id": recorder_id}

    def case_archive_status(self, recorder_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(recorder_id)
        if session is None:
            return {"ok": False, "error": "unknown_recorder"}
        return self._status_dict(session, archive_requested=True)

    def _status_dict(self, session: CaseArchiveSession, *, archive_requested: bool) -> dict[str, Any]:
        with self._lock:
            rc = self._refcount.get(session.symbol)
            refcount = rc.count if rc else 0
            static = bool(rc.static_keeper) if rc else False
        return {
            "ok": True,
            "archive_requested": archive_requested,
            "archive_started": session.archive_started,
            "archive_ready": session.archive_ready and session.snapshot_written,
            "archive_path": session.archive_path,
            "manifest_path": session.manifest_path,
            "archive_format_version": ARCHIVE_FORMAT_VERSION,
            "archive_started_at": session.archive_started_at,
            "snapshot_generation": session.snapshot_generation,
            "start_sequence": session.start_sequence,
            "start_update_id": session.start_update_id,
            "recorder_id": session.recorder_id,
            "case_id": session.case_id,
            "experiment_id": session.experiment_id,
            "symbol": session.symbol,
            "coverage_valid": session.coverage_valid and not session.overflow,
            "overflow": session.overflow,
            "snapshot_written": session.snapshot_written,
            "refcount": refcount,
            "static_keeper_active": static,
            "source": SOURCE_NAME,
            "finalize_error": session.finalize_error,
        }

    def shutdown(self) -> None:
        with self._lock:
            ids = list(self._sessions.keys())
        for rid in ids:
            try:
                self.finalize_case_archive(rid)
            except Exception:
                logger.exception("case_archive_shutdown_finalize_failed")
        self.detach()

    def handle_request(self, req: dict[str, Any]) -> dict[str, Any] | None:
        op = str(req.get("operation") or "").strip().lower()
        request_id = req.get("request_id")
        ops = {
            "start_case_archive",
            "finalize_case_archive",
            "release_case_archive",
            "case_archive_status",
        }
        if op not in ops:
            return None

        def wrap(body: dict[str, Any]) -> dict[str, Any]:
            return {"request_id": request_id, "operation": op, **body}

        try:
            if op == "start_case_archive":
                return wrap(
                    self.start_case_archive(
                        symbol=str(req.get("symbol") or ""),
                        case_id=str(req.get("case_id") or ""),
                        experiment_id=str(req.get("experiment_id") or ""),
                        archive_duration_seconds=float(
                            req.get("archive_duration_seconds") or 300
                        ),
                        archive_root=req.get("archive_root"),
                        archive_enabled=bool(req.get("archive_enabled", True)),
                        recorder_id=req.get("recorder_id"),
                    )
                )
            if op == "finalize_case_archive":
                return wrap(self.finalize_case_archive(str(req.get("recorder_id") or "")))
            if op == "release_case_archive":
                return wrap(self.release_case_archive(str(req.get("recorder_id") or "")))
            if op == "case_archive_status":
                return wrap(self.case_archive_status(str(req.get("recorder_id") or "")))
        except Exception as exc:
            logger.exception("case_archive_request_failed")
            return wrap({"ok": False, "error": "internal_error", "detail": str(exc)})
        return wrap({"ok": False, "error": "unknown_operation"})

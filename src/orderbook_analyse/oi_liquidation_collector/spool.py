"""Append-only durable spool/WAL for OI/liquidation collector rows.

No secrets. Records are JSON lines with checksums. Unacked data is never
auto-deleted. Corrupt trailing records fail closed on replay.

Meta commits are single-owner: one lock, unique temp files, file+dir fsync,
monotone generation, and ack cursors that never move backwards.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


class SpoolError(RuntimeError):
    pass


class SpoolFullError(SpoolError):
    pass


class SpoolCorruptError(SpoolError):
    pass


class SpoolMetaError(SpoolError):
    """Critical metadata commit/load failure (fail-closed)."""


def _canonical_dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, default=str)


def record_checksum(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_dumps(payload).encode("utf-8")).hexdigest()


@dataclass
class SpoolRecord:
    seq: int
    table: str
    record_id: str
    payload: dict[str, Any]
    enqueued_at_unix: float
    checksum: str

    def to_line(self) -> str:
        body = {
            "seq": self.seq,
            "table": self.table,
            "record_id": self.record_id,
            "payload": self.payload,
            "enqueued_at_unix": self.enqueued_at_unix,
            "checksum": self.checksum,
        }
        return _canonical_dumps(body) + "\n"


def make_record_id(table: str, rec: dict[str, Any]) -> str:
    key = rec.get("event_key")
    if key:
        return f"{table}:{key}"
    parts = [
        table,
        str(rec.get("symbol") or ""),
        str(rec.get("bucket_time") or rec.get("event_ts") or rec.get("event_time") or ""),
        str(rec.get("collector_instance_id") or ""),
        str(rec.get("event_type") or ""),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class DurableSpool:
    """Segmented JSONL spool with atomic, serialized ack cursor."""

    def __init__(
        self,
        root: Path,
        *,
        segment_max_bytes: int = 8_000_000,
        max_bytes: int = 512_000_000,
        min_free_bytes: int = 256_000_000,
    ) -> None:
        self.root = Path(root)
        self.segments_dir = self.root / "segments"
        self.meta_path = self.root / "meta.json"
        self.meta_prev_path = self.root / "meta.json.prev"
        self.segment_max_bytes = segment_max_bytes
        self.max_bytes = max_bytes
        self.min_free_bytes = min_free_bytes
        self._lock = threading.RLock()
        self._next_seq = 1
        self._last_acked_seq = 0
        self._meta_generation = 0
        self._active_path: Path | None = None
        self._active_fp = None
        self._acked_ids: set[str] = set()
        self.segments_dir.mkdir(parents=True, exist_ok=True)
        self._quarantine_orphan_tmps()
        self._load_meta()
        self._open_active_for_append()

    def _quarantine_orphan_tmps(self) -> None:
        """Detect orphaned meta temp files. Never delete unacked segment data."""
        for p in self.root.glob("meta.json.tmp*"):
            # Leave content for audit; rename aside so it cannot collide with commits.
            try:
                dest = self.root / f"orphan_{p.name}.{int(time.time())}.{uuid.uuid4().hex[:8]}"
                p.rename(dest)
            except OSError:
                pass

    def _parse_meta_obj(self, meta: dict[str, Any], *, source: Path) -> tuple[int, int, int]:
        try:
            last_acked = int(meta.get("last_acked_seq") or 0)
            next_seq = int(meta.get("next_seq") or (last_acked + 1))
            generation = int(meta.get("generation") or 0)
        except (TypeError, ValueError) as exc:
            raise SpoolCorruptError(f"meta fields invalid in {source}: {exc}") from exc
        if last_acked < 0 or next_seq < 0 or generation < 0:
            raise SpoolCorruptError(f"meta negative cursor in {source}")
        if next_seq <= last_acked:
            next_seq = last_acked + 1
        return last_acked, next_seq, generation

    def _read_meta_file(self, path: Path) -> tuple[int, int, int]:
        try:
            raw = path.read_text(encoding="utf-8")
            meta = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise SpoolCorruptError(f"meta unreadable {path}: {exc}") from exc
        if not isinstance(meta, dict):
            raise SpoolCorruptError(f"meta not an object: {path}")
        return self._parse_meta_obj(meta, source=path)

    def _max_segment_seq(self) -> int:
        max_seq = 0
        for path in self._segment_paths():
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        raw = line.strip()
                        if not raw:
                            continue
                        try:
                            obj = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(obj, dict) and "seq" in obj:
                            max_seq = max(max_seq, int(obj["seq"]))
            except OSError:
                continue
        return max_seq

    def _reconcile_cursors_with_segments(self) -> None:
        max_seg = self._max_segment_seq()
        if max_seg >= self._next_seq:
            self._next_seq = max_seg + 1
        if self._last_acked_seq > max_seg:
            # Ack past data is inconsistent — fail closed.
            raise SpoolCorruptError(
                f"last_acked_seq={self._last_acked_seq} exceeds max segment seq={max_seg}"
            )
        if self._next_seq <= self._last_acked_seq:
            self._next_seq = self._last_acked_seq + 1

    def _load_meta(self) -> None:
        loaded_from_prev = False
        if not self.meta_path.is_file():
            if self.meta_prev_path.is_file():
                self._last_acked_seq, self._next_seq, self._meta_generation = self._read_meta_file(
                    self.meta_prev_path
                )
                loaded_from_prev = True
            else:
                self._persist_meta_unlocked()
                return
        else:
            try:
                self._last_acked_seq, self._next_seq, self._meta_generation = self._read_meta_file(
                    self.meta_path
                )
            except SpoolCorruptError:
                if not self.meta_prev_path.is_file():
                    raise
                self._last_acked_seq, self._next_seq, self._meta_generation = self._read_meta_file(
                    self.meta_prev_path
                )
                loaded_from_prev = True
        self._reconcile_cursors_with_segments()
        if loaded_from_prev:
            self._persist_meta_unlocked()

    def _persist_meta_unlocked(self) -> None:
        """Single meta write path. Caller MUST hold self._lock."""
        self._meta_generation += 1
        payload = {
            "generation": self._meta_generation,
            "last_acked_seq": self._last_acked_seq,
            "next_seq": self._next_seq,
            "updated_unix": time.time(),
        }
        data = (_canonical_dumps(payload) + "\n").encode("utf-8")
        # Unique temp per commit — never share meta.json.tmp across tasks/threads.
        tmp_name = f"meta.json.tmp.{os.getpid()}.{threading.get_ident()}.{self._meta_generation}.{uuid.uuid4().hex}"
        tmp = self.root / tmp_name
        try:
            fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError as exc:
            raise SpoolMetaError(f"meta temp collision: {tmp}") from exc
        try:
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            # Atomic publish first (old meta remains until this succeeds).
            os.replace(tmp, self.meta_path)
            _fsync_dir(self.root)
            # Best-effort previous-good copy AFTER successful publish (no window
            # where meta.json is missing).
            prev_tmp = self.root / f"{tmp_name}.prevwrite"
            try:
                pfd = os.open(str(prev_tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                try:
                    os.write(pfd, data)
                    os.fsync(pfd)
                finally:
                    os.close(pfd)
                os.replace(prev_tmp, self.meta_prev_path)
                _fsync_dir(self.root)
            except OSError:
                try:
                    if prev_tmp.exists():
                        prev_tmp.unlink()
                except OSError:
                    pass
        except OSError as exc:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise SpoolMetaError(f"meta commit failed: {exc}") from exc

    def _persist_meta(self) -> None:
        with self._lock:
            self._persist_meta_unlocked()

    def _disk_usage_bytes(self) -> int:
        total = 0
        for p in self.segments_dir.glob("*.jsonl"):
            try:
                total += p.stat().st_size
            except OSError:
                pass
        return total

    def _free_bytes(self) -> int | None:
        try:
            st = os.statvfs(self.root)
            return int(st.f_bavail * st.f_frsize)
        except OSError:
            return None

    def _check_capacity(self, incoming: int) -> None:
        used = self._disk_usage_bytes()
        if used + incoming > self.max_bytes:
            raise SpoolFullError(
                f"spool max_bytes exceeded used={used} max={self.max_bytes} incoming={incoming}"
            )
        free = self._free_bytes()
        if free is not None and free < self.min_free_bytes:
            raise SpoolFullError(f"filesystem free bytes too low: {free} < {self.min_free_bytes}")

    def _segment_paths(self) -> list[Path]:
        return sorted(self.segments_dir.glob("*.jsonl"))

    def _open_active_for_append(self) -> None:
        paths = self._segment_paths()
        if paths:
            last = paths[-1]
            if last.stat().st_size < self.segment_max_bytes:
                self._active_path = last
                self._active_fp = open(last, "a", encoding="utf-8")
                return
        idx = 1
        if paths:
            try:
                idx = int(paths[-1].stem) + 1
            except ValueError:
                idx = len(paths) + 1
        path = self.segments_dir / f"{idx:09d}.jsonl"
        self._active_path = path
        self._active_fp = open(path, "a", encoding="utf-8")

    def _rotate_if_needed(self) -> None:
        if self._active_path is None or self._active_fp is None:
            self._open_active_for_append()
            return
        self._active_fp.flush()
        if self._active_path.stat().st_size >= self.segment_max_bytes:
            self._active_fp.close()
            self._active_fp = None
            self._open_active_for_append()

    def close(self) -> None:
        with self._lock:
            if self._active_fp is not None:
                try:
                    self._active_fp.flush()
                    os.fsync(self._active_fp.fileno())
                except OSError:
                    pass
                self._active_fp.close()
                self._active_fp = None

    def append(self, table: str, rec: dict[str, Any]) -> SpoolRecord:
        with self._lock:
            if self._active_fp is None:
                self._open_active_for_append()
            assert self._active_fp is not None
            rid = make_record_id(table, rec)
            seq = self._next_seq
            payload = dict(rec)
            checksum = record_checksum(
                {"seq": seq, "table": table, "record_id": rid, "payload": payload}
            )
            item = SpoolRecord(
                seq=seq,
                table=table,
                record_id=rid,
                payload=payload,
                enqueued_at_unix=time.time(),
                checksum=checksum,
            )
            line = item.to_line()
            self._check_capacity(len(line.encode("utf-8")))
            try:
                self._active_fp.write(line)
                self._active_fp.flush()
                os.fsync(self._active_fp.fileno())
            except OSError as exc:
                raise SpoolFullError(f"spool write failed: {exc}") from exc
            self._next_seq = seq + 1
            self._persist_meta_unlocked()
            self._rotate_if_needed()
            return item

    def append_many(self, table: str, recs: list[dict[str, Any]]) -> list[SpoolRecord]:
        return [self.append(table, r) for r in recs]

    def ack(self, seq: int, record_id: str | None = None) -> None:
        with self._lock:
            if seq < self._last_acked_seq:
                # Never move ack cursor backwards.
                return
            self._last_acked_seq = seq
            if record_id:
                self._acked_ids.add(record_id)
            self._persist_meta_unlocked()

    def ack_through(self, seq: int) -> None:
        self.ack(seq)

    def is_acked(self, seq: int) -> bool:
        with self._lock:
            return seq <= self._last_acked_seq

    @property
    def last_acked_seq(self) -> int:
        with self._lock:
            return self._last_acked_seq

    @property
    def next_seq(self) -> int:
        with self._lock:
            return self._next_seq

    @property
    def meta_generation(self) -> int:
        with self._lock:
            return self._meta_generation

    def unacked_stats(self) -> tuple[int, int]:
        with self._lock:
            count = 0
            nbytes = 0
            for rec in self._iter_unacked_unlocked():
                count += 1
                nbytes += len(rec.to_line().encode("utf-8"))
            return count, nbytes

    def _parse_line(self, line: str, *, allow_partial_tail: bool) -> SpoolRecord | None:
        raw = line.strip()
        if not raw:
            return None
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SpoolCorruptError(f"corrupt spool line: {exc}") from exc
        required = ("seq", "table", "record_id", "payload", "checksum")
        if any(k not in obj for k in required):
            raise SpoolCorruptError("spool line missing fields")
        expect = record_checksum(
            {
                "seq": obj["seq"],
                "table": obj["table"],
                "record_id": obj["record_id"],
                "payload": obj["payload"],
            }
        )
        if expect != obj["checksum"]:
            raise SpoolCorruptError(f"checksum mismatch seq={obj.get('seq')}")
        return SpoolRecord(
            seq=int(obj["seq"]),
            table=str(obj["table"]),
            record_id=str(obj["record_id"]),
            payload=dict(obj["payload"]),
            enqueued_at_unix=float(obj.get("enqueued_at_unix") or 0.0),
            checksum=str(obj["checksum"]),
        )

    def iter_all(self) -> Iterator[SpoolRecord]:
        with self._lock:
            yield from self._iter_all_unlocked()

    def _iter_all_unlocked(self) -> Iterator[SpoolRecord]:
        paths = self._segment_paths()
        for i, path in enumerate(paths):
            is_last = i == len(paths) - 1
            with open(path, "r", encoding="utf-8") as fh:
                content = fh.read()
            if not content:
                continue
            lines = content.splitlines(keepends=True)
            for j, line in enumerate(lines):
                is_tail = is_last and j == len(lines) - 1
                if is_tail and not line.endswith("\n"):
                    raise SpoolCorruptError(f"torn trailing record in {path.name}")
                rec = self._parse_line(line, allow_partial_tail=False)
                if rec is not None:
                    yield rec

    def iter_unacked(self) -> Iterator[SpoolRecord]:
        with self._lock:
            yield from list(self._iter_unacked_unlocked())

    def _iter_unacked_unlocked(self) -> Iterator[SpoolRecord]:
        for rec in self._iter_all_unlocked():
            if rec.seq > self._last_acked_seq:
                yield rec

    def truncate_acked_segments(self) -> int:
        with self._lock:
            removed = 0
            paths = self._segment_paths()
            if len(paths) <= 1:
                return 0
            for path in paths[:-1]:
                max_seq = 0
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        for line in fh:
                            rec = self._parse_line(line, allow_partial_tail=False)
                            if rec:
                                max_seq = max(max_seq, rec.seq)
                except SpoolCorruptError:
                    continue
                if max_seq > 0 and max_seq <= self._last_acked_seq:
                    try:
                        path.unlink()
                        removed += 1
                    except OSError:
                        pass
            return removed

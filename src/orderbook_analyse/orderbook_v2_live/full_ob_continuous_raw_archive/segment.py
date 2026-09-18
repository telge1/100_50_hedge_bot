"""Hourly streaming-zstd segments with durable flush and atomic completion."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from .config import FORMAT_VERSION, SCHEMA_VERSION
from .envelope import serialize_envelope

try:
    import zstandard as zstd
except ImportError:  # pragma: no cover
    zstd = None  # type: ignore[assignment]

OPEN = "OPEN"
COMPLETE = "COMPLETE"
PARTIAL = "PARTIAL"
GAP = "GAP"
FAILED_DISK = "FAILED_DISK"


def utc_hour(ts: datetime) -> datetime:
    value = ts.astimezone(timezone.utc)
    return value.replace(minute=0, second=0, microsecond=0)


def segment_directory(root: Path, symbol: str, ts: datetime) -> Path:
    value = ts.astimezone(timezone.utc)
    return root / symbol.upper() / f"{value.year:04d}" / f"{value.month:02d}" / f"{value.day:02d}"


def _iso_ns(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1e9, tz=timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class SegmentCounters:
    message_count: int = 0
    level_update_count: int = 0
    snapshot_count: int = 0
    delta_count: int = 0
    checkpoint_count: int = 0
    gap_count: int = 0
    reconnect_count: int = 0
    overflow_count: int = 0
    uncompressed_bytes: int = 0


class SegmentWriter:
    def __init__(
        self,
        *,
        root: Path,
        symbol: str,
        start_time: datetime,
        archive_instance_id: str,
        collector_instance_id: str,
        compression_level: int = 3,
    ) -> None:
        self.root = Path(root)
        self.symbol = symbol.upper()
        self.start_time = utc_hour(start_time)
        self.archive_instance_id = archive_instance_id
        self.collector_instance_id = collector_instance_id
        self.compression_level = compression_level
        self.directory = segment_directory(self.root, self.symbol, self.start_time)
        stamp = self.start_time.strftime("%Y%m%dT%H0000Z")
        instance_suffix = "".join(c for c in archive_instance_id if c.isalnum())[-12:]
        base = f"{self.symbol}_{stamp}_{instance_suffix}_{FORMAT_VERSION}.ndjson.zst"
        self.tmp_path = self.directory / f"{base}.tmp"
        self.final_path = self.directory / base
        self.counters = SegmentCounters()
        self.first_event_time_ns: int | None = None
        self.last_event_time_ns: int | None = None
        self.first_receive_time_ns: int | None = None
        self.last_receive_time_ns: int | None = None
        self.first_u: int | None = None
        self.last_u: int | None = None
        self.first_seq: int | None = None
        self.last_seq: int | None = None
        self.has_valid_anchor = False
        self.continuity_status = "UNANCHORED"
        self._last_chain_u: int | None = None
        self._seen_book_record = False
        self._content_hash = hashlib.sha256()
        self._fh: BinaryIO | None = None
        self._stream: Any = None
        self._opened = False

    def open(self) -> None:
        if zstd is None:
            raise RuntimeError("zstandard not installed")
        self.directory.mkdir(parents=True, exist_ok=True)
        self._fh = self.tmp_path.open("xb")
        # closefd=False is required so the underlying fd remains fsync-able.
        self._stream = zstd.ZstdCompressor(level=self.compression_level).stream_writer(
            self._fh, closefd=False
        )
        self._opened = True

    def write(self, record: dict[str, Any]) -> None:
        if not self._opened or self._stream is None:
            raise RuntimeError("segment not open")
        line = serialize_envelope(record)
        self._stream.write(line)
        self._content_hash.update(line)
        self.counters.message_count += 1
        self.counters.uncompressed_bytes += len(line)
        event_ns = record.get("event_time_ns")
        receive_ns = record.get("receive_time_ns")
        if event_ns is not None:
            self.first_event_time_ns = self.first_event_time_ns or int(event_ns)
            self.last_event_time_ns = int(event_ns)
        if receive_ns is not None:
            self.first_receive_time_ns = self.first_receive_time_ns or int(receive_ns)
            self.last_receive_time_ns = int(receive_ns)
        kind = str(record.get("message_type") or "")
        payload = record.get("original_payload") if isinstance(record.get("original_payload"), dict) else {}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if kind == "checkpoint":
            self.counters.checkpoint_count += 1
            cp = payload
            if cp.get("checkpoint_reason") in {
                "segment_start",
                "exchange_snapshot",
                "reconnect_resync",
            }:
                self.has_valid_anchor = True
                self.continuity_status = "CONTIGUOUS"
            self._last_chain_u = record.get("u")
            self._seen_book_record = True
        elif kind == "snapshot":
            self.counters.snapshot_count += 1
            if not self._seen_book_record:
                self.has_valid_anchor = True
                self.continuity_status = "CONTIGUOUS"
            self._last_chain_u = record.get("u")
            self._seen_book_record = True
        elif kind == "delta":
            self.counters.delta_count += 1
            self.counters.level_update_count += len(data.get("b") or []) + len(data.get("a") or [])
            current_u = record.get("u")
            if self._last_chain_u is not None and current_u is not None:
                current_u = int(current_u)
                if current_u not in {int(self._last_chain_u), int(self._last_chain_u) + 1}:
                    self.counters.gap_count += 1
                    self.continuity_status = "GAP"
                self._last_chain_u = current_u
            self._seen_book_record = True
        elif kind == "gap_marker":
            self.counters.gap_count += 1
            self.continuity_status = "GAP"
        elif kind == "lifecycle" and str(payload.get("details", {}).get("event") or "").lower() == "reconnect":
            self.counters.reconnect_count += 1
        u, seq = record.get("u"), record.get("seq")
        if u is not None:
            self.first_u = int(u) if self.first_u is None else self.first_u
            self.last_u = int(u)
        if seq is not None:
            self.first_seq = int(seq) if self.first_seq is None else self.first_seq
            self.last_seq = int(seq)

    def note_overflow(self) -> None:
        self.counters.overflow_count += 1

    def flush(self) -> None:
        if not self._opened or self._stream is None or self._fh is None:
            return
        self._stream.flush(zstd.FLUSH_BLOCK)
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def close(
        self,
        *,
        end_time: datetime | None = None,
        clean: bool = True,
        requested_status: str | None = None,
    ) -> tuple[Path, Path, dict[str, Any]]:
        if not self._opened or self._stream is None or self._fh is None:
            raise RuntimeError("segment not open")
        end = end_time or datetime.now(timezone.utc)
        try:
            self._stream.flush(zstd.FLUSH_FRAME)
            self._stream.close()
            self._stream = None
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
            self._fh = None
        finally:
            self._opened = False
        status = requested_status
        if status is None:
            if self.counters.gap_count:
                status = GAP
            elif not clean or not self.has_valid_anchor or self.counters.overflow_count:
                status = PARTIAL
            else:
                status = COMPLETE
        os.replace(self.tmp_path, self.final_path)
        dir_fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        compressed_bytes = self.final_path.stat().st_size
        file_hash = hashlib.sha256(self.final_path.read_bytes()).hexdigest()
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "format_version": FORMAT_VERSION,
            "symbol": self.symbol,
            "utc_hour": self.start_time.strftime("%Y-%m-%dT%H:00:00Z"),
            "segment_start": self.start_time.isoformat().replace("+00:00", "Z"),
            "segment_end": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "first_event_time": _iso_ns(self.first_event_time_ns),
            "last_event_time": _iso_ns(self.last_event_time_ns),
            "first_receive_time": _iso_ns(self.first_receive_time_ns),
            "last_receive_time": _iso_ns(self.last_receive_time_ns),
            **self.counters.__dict__,
            "first_u": self.first_u,
            "last_u": self.last_u,
            "first_seq": self.first_seq,
            "last_seq": self.last_seq,
            "compressed_bytes": compressed_bytes,
            "payload_sha256": self._content_hash.hexdigest(),
            "segment_sha256": file_hash,
            "completion_status": status,
            "has_valid_anchor": self.has_valid_anchor,
            "continuity_status": self.continuity_status,
            "archive_instance_id": self.archive_instance_id,
            "collector_instance_id": self.collector_instance_id,
        }
        manifest_path = Path(str(self.final_path) + ".manifest.json")
        manifest_tmp = Path(str(manifest_path) + ".tmp")
        with manifest_tmp.open("x", encoding="utf-8") as out:
            json.dump(manifest, out, indent=2, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(manifest_tmp, manifest_path)
        return self.final_path, manifest_path, manifest

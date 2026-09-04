"""Streaming compressed segment writer with atomic finalize."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from orderbook_analyse.orderbook_v2_live.raw_archive.config import FORMAT_VERSION, PARSER_VERSION

try:
    import zstandard as zstd
except ImportError:  # pragma: no cover
    zstd = None  # type: ignore[assignment]


@dataclass
class SegmentStats:
    symbol: str
    start_utc: datetime
    end_utc: datetime | None = None
    native_snapshot_count: int = 0
    checkpoint_count: int = 0
    delta_count: int = 0
    marker_count: int = 0
    event_count: int = 0
    first_sequence: int | None = None
    last_sequence: int | None = None
    sequence_gaps: list[tuple[int, int]] = field(default_factory=list)
    first_u: int | None = None
    last_u: int | None = None
    u_gaps: list[tuple[int, int]] = field(default_factory=list)
    queue_overflow: int = 0
    writer_errors: int = 0
    compressed_bytes: int = 0
    uncompressed_bytes: int = 0
    replayable: bool = True
    completion_status: str = "open"
    continuity_status: str = "unknown"
    replay_source: str = "none"
    sha256: str = ""
    _forced_non_replayable: bool = False


class SegmentWriter:
    """Append NDJSON lines to a compressed open segment."""

    def __init__(
        self,
        *,
        symbol: str,
        directory: Path,
        start_utc: datetime,
        compression: str = "zstd",
        compression_level: int = 3,
    ) -> None:
        self.symbol = symbol.upper()
        self.directory = directory
        self.start_utc = start_utc
        self.compression = compression
        self.compression_level = compression_level
        self.stats = SegmentStats(symbol=self.symbol, start_utc=start_utc)
        self._open_path = self._open_filename()
        self._fh: BinaryIO | None = None
        self._compressor = None
        self._sha = hashlib.sha256()
        self._opened = False

    def _stamp(self, ts: datetime) -> str:
        return ts.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def _open_filename(self) -> Path:
        stamp = self._stamp(self.start_utc)
        ext = "zst" if self.compression == "zstd" else "ndjson"
        name = f"{self.symbol}_{stamp}_open_{PARSER_VERSION}.{ext}.tmp"
        return self.directory / name

    def _refresh_replay_source(self) -> None:
        has_native = self.stats.native_snapshot_count > 0
        has_cp = self.stats.checkpoint_count > 0
        if has_native and has_cp:
            self.stats.replay_source = "mixed"
        elif has_cp:
            self.stats.replay_source = "rotation_checkpoint"
        elif has_native:
            self.stats.replay_source = "native_snapshot"
        else:
            self.stats.replay_source = "none"

    def open(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.compression == "zstd":
            if zstd is None:
                raise RuntimeError("zstandard not installed")
            self._fh = open(self._open_path, "wb")  # noqa: SIM115
            self._compressor = zstd.ZstdCompressor(level=self.compression_level).stream_writer(
                self._fh
            )
        else:
            self._fh = open(self._open_path, "wb")  # noqa: SIM115
            self._compressor = self._fh
        self._opened = True
        self.stats.completion_status = "open"

    def write_line(
        self,
        data: bytes,
        *,
        kind: str,
        sequence: int | None = None,
        update_id: int | None = None,
    ) -> None:
        if not self._opened or self._compressor is None:
            raise RuntimeError("segment not open")
        self._compressor.write(data)
        self._sha.update(data)
        self.stats.event_count += 1
        self.stats.uncompressed_bytes += len(data)
        if kind == "snapshot":
            self.stats.native_snapshot_count += 1
        elif kind == "delta":
            self.stats.delta_count += 1
        elif kind == "rotation_checkpoint":
            self.stats.checkpoint_count += 1
        elif kind == "marker":
            self.stats.marker_count += 1
        self._refresh_replay_source()

        # seq is exchange-wide / informational only — never flips replayable.
        if sequence is not None:
            if self.stats.first_sequence is None:
                self.stats.first_sequence = sequence
            self.stats.last_sequence = sequence

        # Book continuity is Bybit data.u (+1 between deltas).
        if update_id is not None and kind in {"snapshot", "delta", "rotation_checkpoint"}:
            if kind in {"snapshot", "rotation_checkpoint"}:
                if self.stats.first_u is None:
                    self.stats.first_u = update_id
                self.stats.last_u = update_id
                if self.stats.continuity_status != "u_gap":
                    self.stats.continuity_status = "contiguous_u"
            elif kind == "delta":
                if self.stats.last_u is None:
                    self.stats.first_u = update_id
                    self.stats.last_u = update_id
                    if self.stats.continuity_status != "u_gap":
                        self.stats.continuity_status = "contiguous_u"
                elif update_id == self.stats.last_u:
                    pass  # duplicate u
                elif update_id == self.stats.last_u + 1:
                    self.stats.last_u = update_id
                    if self.stats.continuity_status != "u_gap":
                        self.stats.continuity_status = "contiguous_u"
                else:
                    self.stats.u_gaps.append((self.stats.last_u, update_id))
                    self.stats.continuity_status = "u_gap"
                    self.stats.replayable = False
                    self.stats.last_u = update_id

        if self.stats._forced_non_replayable:
            self.stats.replayable = False
        elif self.stats.continuity_status == "u_gap":
            self.stats.replayable = False
        elif self.stats.continuity_status == "contiguous_u":
            self.stats.replayable = True

    def mark_non_replayable(self, reason: str) -> None:
        self.stats.replayable = False
        self.stats._forced_non_replayable = True
        # Reason retained for diagnostics; finalize still sets completion_status=closed.
        self.stats.completion_status = reason

    def close(self, *, end_utc: datetime, git_head: str = "") -> tuple[Path, Path]:
        if not self._opened or self._compressor is None:
            raise RuntimeError("segment not open")
        self.stats.end_utc = end_utc
        if self.compression == "zstd" and hasattr(self._compressor, "flush"):
            self._compressor.flush(zstd.FLUSH_FRAME)  # type: ignore[attr-defined]
        self._compressor.close()
        self._compressor = None
        if self._fh is not None and not self._fh.closed:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
        self.stats.compressed_bytes = self._open_path.stat().st_size
        self.stats.sha256 = self._sha.hexdigest()

        ext = "zst" if self.compression == "zstd" else "ndjson"
        final_name = (
            f"{self.symbol}_{self._stamp(self.start_utc)}_{self._stamp(end_utc)}"
            f"_{PARSER_VERSION}.{ext}"
        )
        final_path = self.directory / final_name
        os.replace(self._open_path, final_path)
        # Successful atomic finalize → always closed (independent of replayable).
        self.stats.completion_status = "closed"
        self._refresh_replay_source()

        manifest = {
            "format_version": FORMAT_VERSION,
            "parser_version": PARSER_VERSION,
            "depth": 200,
            "symbol": self.symbol,
            "start_utc": self.start_utc.isoformat().replace("+00:00", "Z"),
            "end_utc": end_utc.isoformat().replace("+00:00", "Z"),
            "native_snapshot_count": self.stats.native_snapshot_count,
            "checkpoint_count": self.stats.checkpoint_count,
            "delta_count": self.stats.delta_count,
            "marker_count": self.stats.marker_count,
            "first_sequence": self.stats.first_sequence,
            "last_sequence": self.stats.last_sequence,
            "sequence_gaps": self.stats.sequence_gaps,
            "first_u": self.stats.first_u,
            "last_u": self.stats.last_u,
            "u_gaps": self.stats.u_gaps,
            "queue_overflow": self.stats.queue_overflow,
            "writer_errors": self.stats.writer_errors,
            "event_count": self.stats.event_count,
            "compressed_bytes": self.stats.compressed_bytes,
            "uncompressed_bytes": self.stats.uncompressed_bytes,
            "sha256": self.stats.sha256,
            "replayable": self.stats.replayable,
            "completion_status": self.stats.completion_status,
            "continuity_status": self.stats.continuity_status,
            "replay_source": self.stats.replay_source,
            "compression": self.compression,
            "collector_git_head": git_head,
        }
        manifest_path = final_path.with_suffix(".manifest.json")
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self._opened = False
        return final_path, manifest_path


def segment_directory(root: Path, symbol: str, ts: datetime) -> Path:
    ts = ts.astimezone(timezone.utc)
    return root / symbol.upper() / f"{ts.year:04d}" / f"{ts.month:02d}" / f"{ts.day:02d}"


def git_head_short() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return out.strip()
    except Exception:
        return ""

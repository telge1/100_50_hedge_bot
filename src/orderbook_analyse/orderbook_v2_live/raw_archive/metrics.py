"""Raw archive metrics (additive health fields)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class RawArchiveMetrics:
    events_received: int = 0
    events_written: int = 0
    events_dropped_overflow: int = 0
    snapshots: int = 0
    deltas: int = 0
    native_snapshots: int = 0
    checkpoint_count: int = 0
    marker_count: int = 0
    gap_count: int = 0
    overflow_count: int = 0
    writer_errors: int = 0
    bytes_written: int = 0
    uncompressed_bytes: int = 0
    queue_high_watermark: int = 0
    paused: bool = False
    segment_replayable: bool = True
    current_segment: str = ""
    last_write_at: datetime | None = None
    last_error: str = ""
    first_sequence: int | None = None
    last_sequence: int | None = None
    sequence_gaps: list[tuple[int, int]] = field(default_factory=list)

    def note_sequence(self, seq: int | None) -> None:
        if seq is None:
            return
        if self.first_sequence is None:
            self.first_sequence = seq
        self.last_sequence = seq

    def to_health(self, *, enabled: bool, symbols: list[str], queue_size: int, current_qsize: int) -> dict:
        return {
            "raw_archive_enabled": enabled,
            "raw_archive_symbols": symbols,
            "raw_queue_size": queue_size,
            "raw_queue_current": current_qsize,
            "raw_queue_high_watermark": self.queue_high_watermark,
            "raw_events_received": self.events_received,
            "raw_events_written": self.events_written,
            "raw_snapshots": self.snapshots,
            "raw_deltas": self.deltas,
            "raw_gap_count": self.gap_count,
            "raw_overflow_count": self.overflow_count,
            "raw_writer_errors": self.writer_errors,
            "raw_last_write_at": self.last_write_at.isoformat().replace("+00:00", "Z")
            if self.last_write_at
            else None,
            "raw_current_segment": self.current_segment,
            "raw_segment_replayable": self.segment_replayable,
            "raw_bytes_written": self.bytes_written,
            "raw_free_disk_gb": None,
            "raw_archive_paused": self.paused,
            "raw_events_dropped_overflow": self.events_dropped_overflow,
        }

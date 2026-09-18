"""Process-lifetime archive health metrics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ArchiveMetrics:
    messages_enqueued: int = 0
    messages_written: int = 0
    level_update_count: int = 0
    checkpoint_count: int = 0
    gap_count: int = 0
    reconnect_count: int = 0
    overflow_count: int = 0
    writer_error_count: int = 0
    flush_count: int = 0
    uncompressed_bytes: int = 0
    completed_segments: int = 0
    partial_segments: int = 0

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

"""Persistent import state machine helpers (local JSON + ClickHouse)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATES = (
    "DISCOVERED",
    "VALIDATING",
    "VALIDATED",
    "IMPORTING",
    "IMPORTED",
    "VERIFYING",
    "VERIFIED",
    "QUARANTINED",
    "FAILED_RETRYABLE",
    "FAILED_PERMANENT",
    "OPEN_NOT_ELIGIBLE",
)

TERMINAL_OK = frozenset({"VERIFIED"})
RETRYABLE = frozenset({"FAILED_RETRYABLE", "IMPORTING", "IMPORTED", "VERIFYING"})


@dataclass
class SegmentImportState:
    segment_id: str
    source_path: str
    source_sha256: str
    file_size: int = 0
    symbol: str = ""
    topic: str = ""
    fight_event_id: str = ""
    segment_index: int = 0
    continuation_index: int = 0
    contract_version: str = "full_ob_finalized_segment_clickhouse_import_v1"
    status: str = "DISCOVERED"
    first_ts: str | None = None
    last_ts: str | None = None
    first_u: int | None = None
    last_u: int | None = None
    first_seq: int | None = None
    last_seq: int | None = None
    record_count: int = 0
    checkpoint_count: int = 0
    continuity_epochs: int = 0
    import_attempts: int = 0
    last_error: str = ""
    import_time: str | None = None
    verify_time: str | None = None
    db_rows_physical: int = 0
    db_rows_logical: int = 0
    replay_status: str = ""
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def bump(self, status: str, **kwargs: Any) -> None:
        self.status = status
        for k, v in kwargs.items():
            setattr(self, k, v)
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SegmentImportState":
        known = set(cls.__dataclass_fields__.keys())  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


class LocalStateStore:
    """Crash-safe local JSON state for resume without relying solely on CH."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            self._data = json.loads(self.path.read_text())

    def get(self, segment_id: str) -> SegmentImportState | None:
        raw = self._data.get(segment_id)
        return SegmentImportState.from_dict(raw) if raw else None

    def put(self, state: SegmentImportState) -> None:
        self._data[state.segment_id] = state.to_dict()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True))
        tmp.replace(self.path)

    def all(self) -> list[SegmentImportState]:
        return [SegmentImportState.from_dict(v) for v in self._data.values()]

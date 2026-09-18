"""Case archive client helpers + fail-closed archive gate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ArchiveGate:
    archive_requested: bool = False
    archive_started: bool = False
    archive_ready: bool = False
    archive_path: str | None = None
    archive_format_version: str | None = None
    recorder_id: str | None = None
    snapshot_generation: int | None = None
    coverage_valid: bool = False
    blocked: bool = False
    block_reason: str | None = None

    def ingest_ack(self, resp: dict[str, Any]) -> None:
        self.archive_requested = bool(resp.get("archive_requested"))
        self.archive_started = bool(resp.get("archive_started"))
        self.archive_ready = bool(resp.get("archive_ready"))
        self.archive_path = resp.get("archive_path") or self.archive_path
        self.archive_format_version = resp.get("archive_format_version")
        self.recorder_id = resp.get("recorder_id") or self.recorder_id
        self.snapshot_generation = resp.get("snapshot_generation")
        self.coverage_valid = bool(resp.get("coverage_valid", False))
        self.validate()

    def validate(self) -> bool:
        if not self.archive_requested:
            self.blocked = True
            self.block_reason = "ARCHIVE_NOT_REQUESTED"
            return False
        if not self.archive_started:
            self.blocked = True
            self.block_reason = "ARCHIVE_NOT_STARTED"
            return False
        if not self.archive_ready:
            self.blocked = True
            self.block_reason = "ARCHIVE_NOT_READY"
            return False
        if not self.archive_format_version:
            self.blocked = True
            self.block_reason = "ARCHIVE_VERSION_UNKNOWN"
            return False
        if not self.coverage_valid:
            self.blocked = True
            self.block_reason = "ARCHIVE_COVERAGE_INVALID"
            return False
        self.blocked = False
        self.block_reason = None
        return True

    def after_finalize(self, resp: dict[str, Any]) -> bool:
        self.archive_path = resp.get("archive_path")
        self.coverage_valid = bool(resp.get("coverage_valid", False)) and bool(resp.get("ok", False))
        if not self.archive_path:
            self.blocked = True
            self.block_reason = "ARCHIVE_PATH_MISSING"
            return False
        if resp.get("error"):
            self.blocked = True
            self.block_reason = "ARCHIVE_FINALIZE_FAILED"
            return False
        return self.validate() or (bool(self.archive_path) and self.coverage_valid)

    def to_dict(self) -> dict[str, Any]:
        return {
            "archive_requested": self.archive_requested,
            "archive_started": self.archive_started,
            "archive_ready": self.archive_ready,
            "archive_path": self.archive_path,
            "archive_format_version": self.archive_format_version,
            "recorder_id": self.recorder_id,
            "snapshot_generation": self.snapshot_generation,
            "coverage_valid": self.coverage_valid,
            "blocked": self.blocked,
            "block_reason": self.block_reason,
            "status": "BLOCKED_RAW_ARCHIVE" if self.blocked else "OK",
        }

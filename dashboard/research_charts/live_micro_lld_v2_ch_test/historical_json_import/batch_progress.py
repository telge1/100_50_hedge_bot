"""Persisted batch import progress report (filesystem JSON, no ClickHouse)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

BatchStatus = Literal["pending", "validated", "written", "readback_ok", "failed", "skipped"]


@dataclass
class CloseOutcome:
    stem: str
    snapshot_id: str
    status: BatchStatus
    detail: str = ""


@dataclass
class BatchProgressReport:
    batch_id: str
    pipeline_line_id: str
    combo_folder: str
    month: str
    import_batch_id: str
    status: BatchStatus
    closes_planned: int
    closes: list[CloseOutcome] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    timing_seconds: dict[str, float] = field(default_factory=dict)
    resource_check_seconds: float | None = None
    error_cause: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "pipeline_line_id": self.pipeline_line_id,
            "combo_folder": self.combo_folder,
            "month": self.month,
            "import_batch_id": self.import_batch_id,
            "status": self.status,
            "closes_planned": self.closes_planned,
            "closes": [asdict(c) for c in self.closes],
            "summary": self.summary,
            "errors": self.errors,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "timing_seconds": self.timing_seconds,
            "resource_check_seconds": self.resource_check_seconds,
            "error_cause": self.error_cause,
        }


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_progress_report(path: Path, report: BatchProgressReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")


def load_progress_report(path: Path) -> BatchProgressReport:
    data = json.loads(path.read_text(encoding="utf-8"))
    closes = [CloseOutcome(**c) for c in data.get("closes") or []]
    return BatchProgressReport(
        batch_id=str(data["batch_id"]),
        pipeline_line_id=str(data["pipeline_line_id"]),
        combo_folder=str(data["combo_folder"]),
        month=str(data["month"]),
        import_batch_id=str(data["import_batch_id"]),
        status=str(data["status"]),
        closes_planned=int(data.get("closes_planned") or 0),
        closes=closes,
        summary=dict(data.get("summary") or {}),
        errors=list(data.get("errors") or []),
        started_at=str(data.get("started_at") or ""),
        finished_at=str(data.get("finished_at") or ""),
        timing_seconds=dict(data.get("timing_seconds") or {}),
        resource_check_seconds=(
            float(data["resource_check_seconds"])
            if data.get("resource_check_seconds") is not None
            else None
        ),
        error_cause=str(data.get("error_cause") or ""),
    )

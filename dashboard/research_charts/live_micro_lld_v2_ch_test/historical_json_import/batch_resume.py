"""Batch success criteria + CH-authoritative resume assessment (read-only helpers)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .batch_progress import BatchProgressReport, BatchStatus, load_progress_report
from .batch_runner import BatchSpec, classify_close_resume
from .historical_batch_config import PILOT_PIPELINE_LINE_ID
from .pilot_overlap import pilot_snapshot_ids_from_catalog
from .provenance_sidecar import InMemoryProvenanceRegistry, ProvenanceSidecarConflictError


@dataclass
class CloseResumeState:
    stem: str
    snapshot_id: str
    classification: str


def assess_close_resume_states(
    stems_and_ids: Sequence[tuple[str, str]],
    *,
    pipeline_line_id: str,
    published_on_line: set[str],
    meta_snapshot_ids: set[str],
    pilot_ids: set[str] | None = None,
) -> list[CloseResumeState]:
    pilot_ids = pilot_ids or set()
    return [
        CloseResumeState(
            stem=stem,
            snapshot_id=sid,
            classification=classify_close_resume(
                snapshot_id=sid,
                pipeline_line_id=pipeline_line_id,
                published_snapshot_ids=published_on_line,
                meta_snapshot_ids=meta_snapshot_ids,
                pilot_snapshot_ids=pilot_ids,
            ),
        )
        for stem, sid in stems_and_ids
    ]


def batch_success_criteria_met(
    *,
    all_published_on_line: bool,
    readback_ok: bool,
    provenance_registry: InMemoryProvenanceRegistry | None,
    snapshot_ids: Sequence[str],
    progress_written: bool,
    pipeline_line_id: str = "",
) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not all_published_on_line:
        errors.append("not all snapshots published on target pipeline line")
    if not readback_ok:
        errors.append("batch readback gate failed")
    if provenance_registry is not None:
        try:
            provenance_registry.require_complete(
                snapshot_ids, pipeline_line_id=pipeline_line_id
            )
        except ProvenanceSidecarConflictError as exc:
            errors.append(str(exc))
    elif snapshot_ids:
        errors.append("provenance registry missing for batch")
    if not progress_written:
        errors.append("progress report not persisted")
    return (not errors, errors)


def reconcile_progress_with_ch(
    progress_path: Path,
    *,
    ch_published_count: int,
    expected_in_batch: int,
) -> dict[str, Any]:
    """
    Progress JSON is auxiliary; ClickHouse publish ledger is authoritative.
    """
    if not progress_path.is_file():
        return {"progress_file": False, "ch_authoritative": True, "warning": "no progress file"}
    prog = load_progress_report(progress_path)
    return {
        "progress_file": True,
        "progress_status": prog.status,
        "progress_closes": len(prog.closes),
        "ch_published_in_batch_scope": ch_published_count,
        "expected": expected_in_batch,
        "ch_authoritative": True,
        "mismatch": prog.status == "readback_ok" and ch_published_count < expected_in_batch,
    }

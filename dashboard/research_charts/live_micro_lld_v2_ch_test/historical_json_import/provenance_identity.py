"""
Canonical original provenance vs import-batch technical identity.

Logical key for batch imports: ``(snapshot_id, pipeline_line_id)``.

``import_batch_id`` is resume metadata only; it must not mask conflicting
original file provenance (sha256, frozen id, path).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .provenance_sidecar import ProvenanceSidecarConflictError, ProvenanceSidecarRow

# Original / forensic fields — must match across retries and physical duplicates.
CANONICAL_ORIGINAL_FIELDS: tuple[str, ...] = (
    "dest_sha256",
    "frozen_input_id",
    "relative_json_path",
    "frozen_price_hash",
    "frozen_revision_hash",
    "catalog_manifest_id",
    "provenance_lookup_version",
)

# Batch metadata compared on idempotent same-batch skip (optional mismatch on resume).
BATCH_METADATA_FIELDS: tuple[str, ...] = ("import_batch_id",)


def assert_canonical_original_match(
    stored: ProvenanceSidecarRow,
    expected: ProvenanceSidecarRow,
    *,
    context: str = "",
) -> None:
    prefix = f"{context}: " if context else ""
    if stored.snapshot_id != expected.snapshot_id:
        raise ProvenanceSidecarConflictError(
            f"{prefix}snapshot_id mismatch {stored.snapshot_id} vs {expected.snapshot_id}"
        )
    if stored.pipeline_line_id != expected.pipeline_line_id:
        raise ProvenanceSidecarConflictError(
            f"{prefix}pipeline_line_id mismatch for {expected.snapshot_id}"
        )
    for field in CANONICAL_ORIGINAL_FIELDS:
        if getattr(stored, field) != getattr(expected, field):
            raise ProvenanceSidecarConflictError(
                f"{prefix}sidecar {field} mismatch for {expected.snapshot_id}"
            )


def rows_canonically_identical(a: ProvenanceSidecarRow, b: ProvenanceSidecarRow) -> bool:
    try:
        assert_canonical_original_match(a, b)
        return True
    except ProvenanceSidecarConflictError:
        return False


@dataclass(frozen=True)
class PhysicalRowEvaluation:
    logical: ProvenanceSidecarRow | None
    physical_count: int
    duplicate_identical: bool


def evaluate_physical_rows(
    rows: Sequence[ProvenanceSidecarRow],
    *,
    snapshot_id: str,
    pipeline_line_id: str,
) -> PhysicalRowEvaluation:
    """
    Collapse physical CH rows to one logical row without ``FINAL`` / ReplacingMergeTree.

    - 0 rows → logical None
    - 1 row → logical that row (after id check)
    - N>1: all must be canonically identical → logical first; else FAIL
    """
    scoped = [
        r
        for r in rows
        if r.snapshot_id == snapshot_id and r.pipeline_line_id == pipeline_line_id
    ]
    if not scoped:
        return PhysicalRowEvaluation(logical=None, physical_count=0, duplicate_identical=False)
    logical = scoped[0]
    duplicate_identical = len(scoped) > 1
    for other in scoped[1:]:
        assert_canonical_original_match(logical, other, context="physical duplicate rows")
    return PhysicalRowEvaluation(
        logical=logical,
        physical_count=len(scoped),
        duplicate_identical=duplicate_identical,
    )


def check_path_binding(
    rows_by_path: Sequence[ProvenanceSidecarRow],
    row: ProvenanceSidecarRow,
) -> None:
    """``relative_json_path`` must not bind to a different snapshot on the same line."""
    for existing in rows_by_path:
        if existing.relative_json_path != row.relative_json_path:
            continue
        if existing.pipeline_line_id != row.pipeline_line_id:
            continue
        if existing.snapshot_id != row.snapshot_id:
            raise ProvenanceSidecarConflictError(
                f"path {row.relative_json_path} bound to {existing.snapshot_id}, "
                f"not {row.snapshot_id}"
            )

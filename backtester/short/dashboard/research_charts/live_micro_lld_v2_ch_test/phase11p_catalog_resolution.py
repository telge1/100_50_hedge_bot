"""Classify a Phase-10 catalog miss without rewriting published rows."""

from __future__ import annotations


def classify_missing_catalog(
    *,
    phase10_line_rows: int,
    snapshot_id_rows: int,
    member_rows: int,
    identity_match: bool,
) -> str:
    """Return CROSS_LINE_FILTER, REAL_DATA_GAP, or UNKNOWN."""
    if snapshot_id_rows <= 0 or member_rows <= 0:
        return "REAL_DATA_GAP"
    if (
        phase10_line_rows == 0
        and snapshot_id_rows == member_rows
        and identity_match
    ):
        return "CROSS_LINE_FILTER"
    if snapshot_id_rows != member_rows or not identity_match:
        return "UNKNOWN"
    return "UNKNOWN"

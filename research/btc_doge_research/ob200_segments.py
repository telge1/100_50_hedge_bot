"""OB200 import segments derived from discovered source files."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .ob200_boundary import BOUNDARY_STATE_AUXILIARY, Ob200FileIndex
from .contracts import sanitize_json, stable_hash
from .full_history_contracts import (
    LIVE_PRODUCER_ID,
    LIVE_RAW_FROM,
    LIVE_TERMINAL,
    OB_SEMANTICS,
    SEGMENT_MISSING,
    SEGMENT_PARTIAL,
    SEGMENT_READY,
    SEGMENT_SOURCE_GAP,
    SHADOW_ARCHIVE_PRODUCER_ID,
    ob_producer_for_hour,
)


class SourceVanishedError(RuntimeError):
    """A source file fingerprinted at plan time disappeared before batch start."""


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def is_zero_duration(file_start: datetime, file_end: datetime) -> bool:
    return file_end <= file_start


def clip_ob_coverage(
    file_start: datetime,
    file_end: datetime,
    *,
    canonical_start: datetime | None = None,
    canonical_end: datetime | None = None,
) -> tuple[datetime, datetime] | None:
    start = file_start
    end = file_end
    if canonical_start is not None:
        start = max(start, canonical_start)
    if canonical_end is not None:
        end = min(end, canonical_end)
    if end <= start:
        return None
    return start, end


def ob_segment_from_file(
    file_row: dict[str, Any],
    *,
    index: Ob200FileIndex | None = None,
) -> dict[str, Any] | None:
    file_start = _parse_ts(file_row["segment_start"])
    file_end = _parse_ts(file_row["segment_end"])
    if file_row.get("zero_duration") or is_zero_duration(file_start, file_end):
        return None

    canonical_start = LIVE_RAW_FROM if file_end > LIVE_RAW_FROM else None
    clipped = clip_ob_coverage(file_start, file_end, canonical_start=canonical_start)
    if clipped is None:
        return None
    coverage_start, coverage_end = clipped

    producer_info = ob_producer_for_hour(file_start, file_exists=True)
    if not producer_info:
        return None
    producer_id, semantics = producer_info
    if producer_id == LIVE_PRODUCER_ID and coverage_start >= LIVE_TERMINAL:
        return None

    expected_rows = int((coverage_end - coverage_start).total_seconds())
    if expected_rows <= 0:
        return None

    full_file_seconds = int((file_end - file_start).total_seconds())
    status = SEGMENT_READY if expected_rows == full_file_seconds and expected_rows == 3600 else SEGMENT_PARTIAL
    if expected_rows < 3600 and file_start < LIVE_RAW_FROM.replace(hour=0, minute=0, second=0, microsecond=0):
        status = SEGMENT_PARTIAL

    stub = index.stub_for_hour(file_row["symbol"], file_start) if index else None
    boundary_meta = {
        "boundary_role": BOUNDARY_STATE_AUXILIARY if stub else "",
        "boundary_auxiliary_path": stub["relative_path"] if stub else "",
        "boundary_auxiliary_fingerprint": stub.get("source_fingerprint", "") if stub else "",
    }

    return sanitize_json(
        {
            "symbol": file_row["symbol"],
            "modality": "OB200",
            "segment_start": coverage_start,
            "segment_end": coverage_end,
            "file_start": file_start,
            "file_end": file_end,
            "producer_id": producer_id,
            "source_semantics": semantics,
            "source": "filesystem_ob200_shadow",
            "source_path": file_row["relative_path"],
            "source_fingerprint": file_row["source_fingerprint"],
            "expected_rows": expected_rows,
            "status": status,
            "actual_rows": expected_rows if status == SEGMENT_READY else 0,
            "exclusion_reason": "",
            "bytes": file_row.get("bytes", 0),
            **boundary_meta,
        }
    )


def ob_inventory_gaps_for_day(symbol: str, day: datetime, files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Non-importable gap markers for hours without file coverage."""
    gaps: list[dict[str, Any]] = []
    day_end = day + timedelta(days=1)
    if day == LIVE_RAW_FROM.replace(hour=0, minute=0, second=0, microsecond=0):
        if LIVE_RAW_FROM > day:
            gaps.append(
                sanitize_json(
                    {
                        "symbol": symbol,
                        "modality": "OB200",
                        "segment_start": day,
                        "segment_end": LIVE_RAW_FROM,
                        "producer_id": "",
                        "source": "filesystem_ob200_shadow",
                        "source_path": "",
                        "source_fingerprint": "",
                        "expected_rows": 0,
                        "status": SEGMENT_MISSING,
                        "exclusion_reason": "NO_PRODUCER_BEFORE_LIVE_RAW_FROM",
                    }
                )
            )
    covered_ranges = []
    for file_row in files:
        seg = ob_segment_from_file(file_row, index=Ob200FileIndex.from_discovery(files))
        if seg:
            covered_ranges.append((seg["segment_start"], seg["segment_end"]))
    return gaps


def build_ob200_segments_from_discovery(ob200_files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    index = Ob200FileIndex.from_discovery(ob200_files)
    segments: list[dict[str, Any]] = []
    for file_row in ob200_files:
        if file_row.get("zero_duration"):
            continue
        seg = ob_segment_from_file(file_row, index=index)
        if seg is None:
            continue
        segments.append(seg)
    return segments


def validate_source_file_at_batch(
    row: dict[str, Any],
) -> dict[str, Any]:
    from pathlib import Path

    from .config import OB200_ROOT
    from .source_file_registry import load_source_file

    relative = row.get("source_path") or ""
    if not relative:
        raise FileNotFoundError("missing source_path for OB200 segment")
    path = OB200_ROOT / relative
    planned_fp = str(row.get("source_fingerprint") or "")
    if not path.is_file():
        if planned_fp:
            raise SourceVanishedError(f"planned source vanished: {relative}")
        raise FileNotFoundError(f"source missing: {path}")
    source = load_source_file(path, OB200_ROOT)
    if planned_fp and source.fingerprint != planned_fp:
        raise SourceVanishedError(
            f"source fingerprint changed for {relative}: planned={planned_fp[:16]} actual={source.fingerprint[:16]}"
        )
    return {
        "path": str(path),
        "relative_path": source.relative_path,
        "fingerprint": source.fingerprint,
        "segment_start": source.segment_start,
        "segment_end": source.segment_end,
    }

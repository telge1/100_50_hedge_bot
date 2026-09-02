"""Coverage rows for bounded pilot windows."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .contracts import PROCESSOR_VERSION, RESEARCH_CONTRACT_VERSION, stable_hash

COVERAGE_COLUMNS = (
    "coverage_key", "source_id", "symbol", "data_type", "period_start",
    "period_end", "expected_buckets", "present_buckets", "genuine_buckets",
    "carried_forward_buckets", "gap_count", "duplicate_count",
    "quality_status", "contract_version", "processor_version",
    "ingestion_batch_id", "checked_at",
)


def coverage_row(
    *,
    source_id: str,
    symbol: str,
    data_type: str,
    start: datetime,
    end: datetime,
    expected: int,
    present: int,
    genuine: int,
    carried_forward: int,
    duplicates: int,
    batch_id: str,
    checked_at: datetime,
) -> tuple[Any, ...]:
    gaps = max(expected - present, 0)
    status = "COMPLETE" if gaps == 0 and duplicates == 0 else "EXPLICIT_GAP"
    key = stable_hash(
        {
            "source_id": source_id,
            "symbol": symbol,
            "data_type": data_type,
            "start": start,
            "end": end,
            "batch_id": batch_id,
        }
    )
    return (
        key, source_id, symbol, data_type, start, end, expected, present,
        genuine, carried_forward, gaps, duplicates, status,
        RESEARCH_CONTRACT_VERSION, PROCESSOR_VERSION, batch_id, checked_at,
    )

"""Deterministic ingestion batch identity."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .contracts import PROCESSOR_VERSION, RESEARCH_CONTRACT_VERSION, stable_hash

BATCH_COLUMNS = (
    "batch_id", "input_fingerprint", "contract_version", "processor_version",
    "symbol", "window_start", "window_end", "status", "rows_written",
    "manifest_fingerprint", "started_at", "completed_at", "error",
)


def input_fingerprint(
    *,
    pilot_id: str,
    symbol: str,
    start: datetime,
    end: datetime,
    source_fingerprints: list[str],
) -> str:
    return stable_hash(
        {
            "pilot_id": pilot_id,
            "symbol": symbol,
            "start": start,
            "end": end,
            "source_fingerprints": sorted(source_fingerprints),
            "contract_version": RESEARCH_CONTRACT_VERSION,
            "processor_version": PROCESSOR_VERSION,
        }
    )


def batch_row(
    *,
    batch_id: str,
    fingerprint: str,
    symbol: str,
    start: datetime,
    end: datetime,
    rows_written: int,
    manifest: dict[str, Any],
    started_at: datetime,
    completed_at: datetime,
) -> tuple[Any, ...]:
    return (
        batch_id,
        fingerprint,
        RESEARCH_CONTRACT_VERSION,
        PROCESSOR_VERSION,
        symbol,
        start,
        end,
        "COMPLETE",
        rows_written,
        stable_hash(manifest),
        started_at,
        completed_at,
        "",
    )

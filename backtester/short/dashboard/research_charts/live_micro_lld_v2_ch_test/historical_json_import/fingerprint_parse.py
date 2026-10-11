"""Parse canonical row_fingerprint strings from historical JSON snapshots."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _parse_optional_dt(text: str | None) -> datetime | None:
    if text is None or str(text).strip() in ("", "None"):
        return None
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(timezone.utc)


def pool_type_from_pool_id(pool_id: str) -> str:
    parts = str(pool_id).split(":")
    if len(parts) < 5:
        raise ValueError(f"cannot derive pool_type from pool_id {pool_id!r}")
    return parts[3]


def enrich_pool_row_from_json(pool_row: dict[str, Any], *, active: bool = True) -> dict[str, Any]:
    """Build a row dict compatible with serialize.row_fingerprint."""
    row = dict(pool_row)
    row["pool_type"] = pool_type_from_pool_id(str(row["pool_id"]))
    row["active"] = 1 if active else 0
    parts = str(row.get("row_fingerprint") or "").split("|")
    if len(parts) != 17:
        raise ValueError(f"unexpected row_fingerprint segments for {row.get('pool_id')}: {len(parts)}")
    row["invalidated_at"] = _parse_optional_dt(parts[9])
    row["pivot_timestamp"] = _parse_optional_dt(parts[11])
    row["early_timing"] = int(parts[12]) if parts[12] not in ("", "None") else None
    if parts[13] in ("", "None"):
        row["reclaim_level"] = None
    else:
        row["reclaim_level"] = float(parts[13])
    row["candidate_created_at"] = _parse_optional_dt(parts[14])
    row["confirmed_at"] = _parse_optional_dt(parts[15])
    row["created_index"] = int(parts[16])
    return row

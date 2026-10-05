"""UTC-only datetime normalization for registry → ClickHouse (no host TZ)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

UTC = timezone.utc


def ensure_utc_datetime(value: Any) -> datetime | None:
    """
    Normalize registry/ISO values to timezone-aware UTC.

    Rules:
    - aware → astimezone(UTC)
    - ISO with Z or offset → parse → UTC
    - naive → treat as UTC wall clock (never local system TZ)
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def ch_datetime64_utc(value: Any) -> datetime | None:
    """ClickHouse DateTime64('UTC') insert value: always aware UTC."""
    return ensure_utc_datetime(value)


def version_from_utc_datetime(value: Any) -> int:
    dt = ensure_utc_datetime(value)
    if dt is None:
        return 0
    return int(dt.timestamp() * 1000)

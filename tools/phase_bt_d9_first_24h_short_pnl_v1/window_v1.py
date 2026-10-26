"""BT-D9 decision-close window (half-open end)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def iter_5m_closes_half_open(start_utc: str, end_utc_exclusive: str) -> list[str]:
    """[start, end) — 24h with end 2026-09-12T12:00:00Z exclusive → last close 11:55."""
    start = datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
    end = datetime.fromisoformat(end_utc_exclusive.replace("Z", "+00:00"))
    if end <= start:
        return []
    cur = start
    out: list[str] = []
    while cur < end:
        out.append(cur.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        cur += timedelta(minutes=5)
    return out

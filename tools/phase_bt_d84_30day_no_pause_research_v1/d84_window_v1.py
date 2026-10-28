"""BT-D84 thirty-day 5m decision-close window (UTC inclusive)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

WINDOW_START_UTC = "2026-09-01T00:00:00Z"
WINDOW_END_UTC = "2026-09-30T23:55:00Z"
EXPECTED_CLOSE_COUNT = 8640


def iter_window_closes_inclusive() -> list[str]:
    start = datetime.fromisoformat(WINDOW_START_UTC.replace("Z", "+00:00"))
    end = datetime.fromisoformat(WINDOW_END_UTC.replace("Z", "+00:00"))
    cur = start
    out: list[str] = []
    while cur <= end:
        out.append(cur.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        cur += timedelta(minutes=5)
    return out


def plan_calendar_day_chunks(closes: list[str]) -> list[list[str]]:
    buckets: dict[str, list[str]] = {}
    for c in closes:
        day = c[:10]
        buckets.setdefault(day, []).append(c)
    return [buckets[k] for k in sorted(buckets)]


def chunk_boundary_starts() -> list[str]:
    """24-close windows spanning each UTC day boundary (23:30 → next day 01:25)."""
    closes = iter_window_closes_inclusive()
    days = sorted({c[:10] for c in closes})
    out: list[str] = []
    for day in days[:-1]:
        start = f"{day}T23:30:00Z"
        if start in closes:
            out.append(start)
    return out


def interior_sample_closes() -> list[str]:
    """One interior close per calendar day (noon UTC)."""
    closes = set(iter_window_closes_inclusive())
    days = sorted({c[:10] for c in closes})
    samples = [f"{d}T12:00:00Z" for d in days]
    return [s for s in samples if s in closes]

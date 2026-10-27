"""BT-D80 three-day 5m decision-close window (UTC inclusive)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

WINDOW_START_UTC = "2026-09-23T00:00:00Z"
WINDOW_END_UTC = "2026-09-25T23:55:00Z"
EXPECTED_CLOSE_COUNT = 864


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
    """Split into UTC calendar days (each ≤72h causal vis span for chunk build)."""
    buckets: dict[str, list[str]] = {}
    for c in closes:
        day = c[:10]
        buckets.setdefault(day, []).append(c)
    return [buckets[k] for k in sorted(buckets)]

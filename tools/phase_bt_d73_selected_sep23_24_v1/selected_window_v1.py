"""BT-D73 user-selected inclusive 5m decision-close window (UTC)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

WINDOW_START_UTC = "2026-09-23T06:45:00Z"
WINDOW_END_UTC = "2026-09-24T14:50:00Z"
EXPECTED_CLOSE_COUNT = 386


def iter_window_closes_inclusive() -> list[str]:
    start = datetime.fromisoformat(WINDOW_START_UTC.replace("Z", "+00:00"))
    end = datetime.fromisoformat(WINDOW_END_UTC.replace("Z", "+00:00"))
    if end < start:
        return []
    cur = start
    out: list[str] = []
    while cur <= end:
        out.append(cur.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        cur += timedelta(minutes=5)
    return out

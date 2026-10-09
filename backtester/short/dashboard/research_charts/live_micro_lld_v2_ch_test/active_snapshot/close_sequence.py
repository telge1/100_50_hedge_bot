"""Deterministic 5m decision-close sequences (research)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ...service import _timeframe_seconds


def enumerate_close_range(
    range_start: datetime,
    range_end_exclusive: datetime,
    timeframe: str = "5m",
) -> list[datetime]:
    step = _timeframe_seconds(timeframe)
    start = range_start.astimezone(timezone.utc)
    end = range_end_exclusive.astimezone(timezone.utc)
    if start >= end:
        raise ValueError("invalid close range")
    out: list[datetime] = []
    t = start
    while t < end:
        out.append(t)
        t += timedelta(seconds=step)
    return out


def missing_closes(
    expected: list[datetime],
    published: list[datetime],
) -> list[datetime]:
    pub = {p.astimezone(timezone.utc) for p in published}
    return [e for e in expected if e.astimezone(timezone.utc) not in pub]


def extra_closes(
    expected: list[datetime],
    published: list[datetime],
) -> list[datetime]:
    exp = {e.astimezone(timezone.utc) for e in expected}
    return [p for p in published if p.astimezone(timezone.utc) not in exp]

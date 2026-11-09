"""UTC calendar-month rolling history (research only)."""

from __future__ import annotations

import calendar
from datetime import datetime, timezone

HISTORY_POLICY_ROLLING_4M = "ROLLING_4_CALENDAR_MONTHS"
DOGE_DATA_EARLIEST = datetime(2025, 12, 11, 0, 0, tzinfo=timezone.utc)


def subtract_calendar_months(dt: datetime, months: int) -> datetime:
    dt = dt.astimezone(timezone.utc)
    y, m = dt.year, dt.month
    m -= int(months)
    while m <= 0:
        m += 12
        y -= 1
    d = min(dt.day, calendar.monthrange(y, m)[1])
    return dt.replace(year=y, month=m, day=d)


def rolling_history_from_close(decision_close: datetime, months: int = 4) -> datetime:
    return subtract_calendar_months(decision_close.astimezone(timezone.utc), months)


def assert_rolling_coverage(
    decision_close: datetime,
    *,
    months: int = 4,
    data_earliest: datetime = DOGE_DATA_EARLIEST,
) -> datetime:
    fc = rolling_history_from_close(decision_close, months)
    if fc < data_earliest:
        raise ValueError(
            f"OUT_OF_COVERAGE: history_from_close {fc.isoformat()} "
            f"before data earliest {data_earliest.isoformat()}"
        )
    return fc

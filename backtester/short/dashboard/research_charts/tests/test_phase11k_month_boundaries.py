"""Phase 11K — UTC calendar-month rolling boundaries (synthetic)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from research_charts.live_micro_lld_v2_ch_test.active_snapshot.rolling_calendar import (
    rolling_history_from_close,
    subtract_calendar_months,
)


@pytest.mark.parametrize(
    "decision,months,expected",
    [
        (datetime(2024, 3, 31, 12, 0, tzinfo=timezone.utc), 1, datetime(2024, 2, 29, 12, 0, tzinfo=timezone.utc)),
        (datetime(2024, 3, 31, 12, 0, tzinfo=timezone.utc), 4, datetime(2023, 11, 30, 12, 0, tzinfo=timezone.utc)),
        (datetime(2025, 1, 31, 0, 0, tzinfo=timezone.utc), 1, datetime(2024, 12, 31, 0, 0, tzinfo=timezone.utc)),
        (datetime(2025, 3, 31, 23, 55, tzinfo=timezone.utc), 1, datetime(2025, 2, 28, 23, 55, tzinfo=timezone.utc)),
        (datetime(2024, 5, 31, 0, 0, tzinfo=timezone.utc), 1, datetime(2024, 4, 30, 0, 0, tzinfo=timezone.utc)),
        (datetime(2026, 2, 28, 12, 0, tzinfo=timezone.utc), 1, datetime(2026, 1, 28, 12, 0, tzinfo=timezone.utc)),
    ],
)
def test_subtract_calendar_months_clamping(decision, months, expected):
    got = subtract_calendar_months(decision, months)
    assert got == expected


def test_rolling_history_from_close_matches_subtract():
    dc = datetime(2026, 9, 30, 21, 35, tzinfo=timezone.utc)
    assert rolling_history_from_close(dc, 4) == subtract_calendar_months(dc, 4)


def test_utc_day_boundary_unchanged_hour():
    dc = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    hf = rolling_history_from_close(dc, 4)
    assert hf.month == 9 and hf.year == 2025

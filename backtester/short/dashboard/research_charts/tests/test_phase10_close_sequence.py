"""Phase 10 close sequence tests."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_dash = Path(__file__).resolve().parents[1]
for p in (_dash.parents[2], _dash):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from research_charts.live_micro_lld_v2_ch_test.active_snapshot.close_sequence import (
    enumerate_close_range,
)


def test_seven_day_close_count():
    start = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
    closes = enumerate_close_range(start, end, "5m")
    assert len(closes) == 2016
    assert closes[0] == start
    assert closes[-1].isoformat() == "2026-09-30T23:55:00+00:00"

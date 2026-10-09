"""Phase 9 rolling calendar and identity tests (no CH)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_dash = Path(__file__).resolve().parents[1]
_repo = _dash.parents[2]
for p in (_repo, _dash):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from research_charts.live_micro_lld_v2_ch_test.active_snapshot.rolling_calendar import (
    DOGE_DATA_EARLIEST,
    assert_rolling_coverage,
    rolling_history_from_close,
    subtract_calendar_months,
)
from research_charts.live_micro_lld_v2_ch_test.identity import engine_input_hash


def test_subtract_calendar_months_march31():
    dt = datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc)
    back = subtract_calendar_months(dt, 1)
    assert back.month == 2
    assert back.day == 28


def test_rolling_from_close():
    T = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    fc = rolling_history_from_close(T, 4)
    assert fc == datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)


def test_out_of_coverage_raises():
    T = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)
    try:
        assert_rolling_coverage(T, months=4, data_earliest=DOGE_DATA_EARLIEST)
        assert False
    except ValueError as e:
        assert "OUT_OF_COVERAGE" in str(e)


def test_engine_input_hash_includes_history_policy():
    doc = {
        "symbol": "DOGEUSDT",
        "timeframe": "5m",
        "pool_detail": 10,
        "engine_version": "live_micro_lld_v2@trp",
        "candle_source": "x",
        "config_hash": "h",
        "history_policy": "ROLLING_4_CALENDAR_MONTHS",
        "from_close": "a",
        "to_close": "b",
        "candle_load_start": "c",
        "warmup_bars": 1,
        "candle_count": 1,
        "candle_fingerprint": "f",
    }
    doc2 = dict(doc)
    doc2["history_policy"] = "OTHER"
    assert engine_input_hash(doc) != engine_input_hash(doc2)

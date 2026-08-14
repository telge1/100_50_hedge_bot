from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from pool_order_plan_v1.candles import (
    FiveMinuteSeries,
    LastFiveIncomplete,
    causal_prefix,
    expected_last_closed_5m,
)


UTC = timezone.utc


def _bar(open_dt: datetime) -> dict:
    close_dt = open_dt + timedelta(minutes=5)
    return {
        "timestamp": pd.Timestamp(open_dt),
        "close_time": pd.Timestamp(close_dt),
        "open": 10.0,
        "high": 11.0,
        "low": 9.0,
        "close": 10.5,
        "volume": 1.0,
    }


def _series(opens: list[datetime]) -> FiveMinuteSeries:
    df = pd.DataFrame([_bar(o) for o in opens])
    return FiveMinuteSeries(
        symbol="TESTUSDT",
        bars=df,
        one_minute_rows=len(opens) * 5,
        missing_one_minute_rows=0,
        duplicate_one_minute_rows=0,
        dropped_incomplete_five_minute_buckets=0,
        history_start=opens[0],
        history_end=opens[-1],
    )


def test_expected_bucket_0117():
    et = datetime(2026, 8, 11, 1, 17, tzinfo=UTC)
    open_t, close_t = expected_last_closed_5m(et)
    assert open_t == datetime(2026, 8, 11, 1, 10, tzinfo=UTC)
    assert close_t == datetime(2026, 8, 11, 1, 15, tzinfo=UTC)


def test_prefix_excludes_running_bar_and_keeps_full_history():
    start = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
    opens = [start + timedelta(minutes=5 * i) for i in range(2500)]  # >> 1000 five-minute bars
    series = _series(opens)
    et = datetime(2026, 8, 11, 1, 17, tzinfo=UTC)
    prefix = causal_prefix(series, et)
    assert len(prefix) > 1000
    last = pd.Timestamp(prefix.iloc[-1]["timestamp"]).to_pydatetime()
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    else:
        last = last.astimezone(UTC)
    assert last == datetime(2026, 8, 11, 1, 10, tzinfo=UTC)
    forbidden = datetime(2026, 8, 11, 1, 15, tzinfo=UTC)
    stamps = [pd.Timestamp(x).tz_convert("UTC").to_pydatetime() for x in prefix["timestamp"]]
    assert forbidden not in stamps


def test_incomplete_expected_bucket_no_fallback():
    start = datetime(2026, 8, 11, 0, 0, tzinfo=UTC)
    opens = [start + timedelta(minutes=5 * i) for i in range(14)]  # up to 01:05
    series = _series(opens)
    et = datetime(2026, 8, 11, 1, 17, tzinfo=UTC)
    with pytest.raises(LastFiveIncomplete):
        causal_prefix(series, et)

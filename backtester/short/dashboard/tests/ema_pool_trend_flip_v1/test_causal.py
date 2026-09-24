from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from ema_pool_trend_flip_v1.ema_regime import indicators_for_bars
from ema_pool_trend_flip_v1.tf_bars import causal_tf_prefix
from pool_order_plan_v1.candles import FutureBarInFrame, LastFiveIncomplete, causal_prefix
from pool_order_plan_v1.candles import FiveMinuteSeries
from pool_order_plan_v1.pool_snapshot import reset_pool_engine_run_count, run_pools_once, pool_engine_run_count


UTC = timezone.utc


def _bar(open_dt: datetime, px: float = 10.0) -> dict:
    close_dt = open_dt + timedelta(minutes=5)
    return {
        "timestamp": pd.Timestamp(open_dt),
        "close_time": pd.Timestamp(close_dt),
        "open": px,
        "high": px + 1,
        "low": px - 1,
        "close": px + 0.2,
        "volume": 1.0,
    }


def test_causal_tf_prefix_excludes_future():
    start = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
    rows = []
    for i in range(10):
        ot = start + timedelta(minutes=15 * i)
        rows.append(
            {
                "timestamp": pd.Timestamp(ot),
                "close_time": pd.Timestamp(ot + timedelta(minutes=15)),
                "open": 1.0,
                "high": 1.1,
                "low": 0.9,
                "close": 1.05,
                "volume": 1.0,
            }
        )
    df = pd.DataFrame(rows)
    et = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
    prefix = causal_tf_prefix(df, et)
    for ct in prefix["close_time"]:
        ts = pd.Timestamp(ct)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        assert ts.to_pydatetime() <= et


def test_ema_uses_only_prefix_length():
    closes = [100.0 + i * 0.1 for i in range(50)]
    highs = [c + 0.2 for c in closes]
    lows = [c - 0.2 for c in closes]
    a = indicators_for_bars(closes[:40], highs[:40], lows[:40])
    b = indicators_for_bars(closes, highs, lows)
    assert a[-1].ema9 != b[-1].ema9


def test_pool_engine_once(monkeypatch):
    start = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
    opens = [start + timedelta(minutes=5 * i) for i in range(80)]
    df = pd.DataFrame([_bar(o, 10 + i * 0.01) for i, o in enumerate(opens)])
    reset_pool_engine_run_count()
    run_pools_once(df)
    run_pools_once(df)
    # caller contract: batch resets and runs once; engine counter increments per call
    assert pool_engine_run_count() == 2
    reset_pool_engine_run_count()
    run_pools_once(df)
    assert pool_engine_run_count() == 1


def test_5m_prefix_no_incomplete_fallback():
    start = datetime(2026, 8, 11, 0, 0, tzinfo=UTC)
    opens = [start + timedelta(minutes=5 * i) for i in range(14)]
    df = pd.DataFrame([_bar(o) for o in opens])
    series = FiveMinuteSeries(
        symbol="ACEUSDT",
        bars=df,
        one_minute_rows=70,
        missing_one_minute_rows=0,
        duplicate_one_minute_rows=0,
        dropped_incomplete_five_minute_buckets=0,
        history_start=opens[0],
        history_end=opens[-1],
    )
    et = datetime(2026, 8, 11, 1, 17, tzinfo=UTC)
    try:
        causal_prefix(series, et)
        raised = False
    except LastFiveIncomplete:
        raised = True
    except FutureBarInFrame:
        raised = True
    assert raised

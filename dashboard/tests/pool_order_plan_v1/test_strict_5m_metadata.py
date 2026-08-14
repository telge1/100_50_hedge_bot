from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from pool_order_plan_v1.candles import (
    build_five_minute_series,
    expected_last_closed_5m,
)
from pool_order_plan_v1.coverage import coverage_row
from pool_order_plan_v1.planner_client import call_plan_orders
from pool_order_plan_v1.pool_snapshot import (
    FiveMinuteFrameError,
    assert_five_minute_frame,
    reset_pool_engine_run_count,
    run_pools_once,
)
from pool_order_plan_v1.research_feed import research_signals_response
from pool_order_plan_v1.schema import (
    POOL_INTERVAL,
    SOURCE_INTERVAL,
    is_confirmed_5m_pool_run,
    last_5m_close_from_open,
    pool_pipeline_stamp,
)
from pool_order_plan_v1.signals import load_closed_1m

UTC = timezone.utc
DASHBOARD = Path(__file__).resolve().parents[2]
ACE_MANIFEST = (
    DASHBOARD.parent
    / "results"
    / "pool_order_plan_v1_comparisons"
    / "aceusdt_48h_20260814T150343Z"
    / "pool_artifacts"
    / "20260814T150343Z-76587402"
    / "manifest.json"
)


def _1m(ot: datetime, *, o=10.0, h=11.0, l=9.0, c=10.5, v=2.0) -> dict:
    return {
        "open_time": ot,
        "close_time": ot + timedelta(minutes=1),
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": v,
    }


def test_five_contiguous_1m_become_one_5m_ohlcv_utc():
    start = datetime(2026, 8, 11, 1, 10, tzinfo=UTC)
    rows = [
        _1m(start, o=10.0, h=10.2, l=9.9, c=10.1, v=1),
        _1m(start + timedelta(minutes=1), o=10.1, h=12.0, l=10.0, c=11.0, v=2),
        _1m(start + timedelta(minutes=2), o=11.0, h=11.1, l=8.0, c=8.5, v=3),
        _1m(start + timedelta(minutes=3), o=8.5, h=9.0, l=8.4, c=8.8, v=4),
        _1m(start + timedelta(minutes=4), o=8.8, h=9.5, l=8.7, c=9.2, v=5),
    ]
    series = build_five_minute_series("TESTUSDT", rows)
    assert len(series.bars) == 1
    bar = series.bars.iloc[0]
    assert pd.Timestamp(bar["timestamp"]).tz_convert("UTC").to_pydatetime() == start
    assert pd.Timestamp(bar["close_time"]).tz_convert("UTC").to_pydatetime() == start + timedelta(minutes=5)
    assert float(bar["open"]) == 10.0
    assert float(bar["high"]) == 12.0
    assert float(bar["low"]) == 8.0
    assert float(bar["close"]) == 9.2
    assert float(bar["volume"]) == 15.0


def test_missing_minute_drops_bucket():
    start = datetime(2026, 8, 11, 1, 10, tzinfo=UTC)
    rows = [
        _1m(start + timedelta(minutes=i))
        for i in (0, 1, 2, 4)  # missing 01:13
    ]
    series = build_five_minute_series("TESTUSDT", rows)
    assert len(series.bars) == 0
    assert series.dropped_incomplete_five_minute_buckets >= 1


def test_duplicate_minute_is_counted_and_coverage_duplicates():
    start = datetime(2026, 8, 11, 1, 10, tzinfo=UTC)
    rows = [_1m(start + timedelta(minutes=i)) for i in range(5)]
    rows.append(_1m(start, o=99.0))
    series = build_five_minute_series("TESTUSDT", rows)
    assert series.duplicate_one_minute_rows >= 1
    cov = coverage_row("TESTUSDT", series, entry_count=1)
    assert cov["coverage_status"] == "DUPLICATES"


def test_open_1m_query_requires_is_closed():
    src = inspect.getsource(load_closed_1m)
    assert "is_closed = 1" in src
    assert 'interval": "1m"' in src or "interval = {interval:String}" in src


def test_running_5m_excluded_when_last_1m_incomplete():
    start = datetime(2026, 8, 11, 1, 10, tzinfo=UTC)
    rows = [_1m(start + timedelta(minutes=i)) for i in range(4)]  # through 01:13, no 01:14
    series = build_five_minute_series("TESTUSDT", rows)
    assert len(series.bars) == 0


def test_entry_0117_last_pool_bar_0110_0115():
    et = datetime(2026, 8, 11, 1, 17, tzinfo=UTC)
    open_t, close_t = expected_last_closed_5m(et)
    assert open_t == datetime(2026, 8, 11, 1, 10, tzinfo=UTC)
    assert close_t == datetime(2026, 8, 11, 1, 15, tzinfo=UTC)
    assert last_5m_close_from_open(open_t) == close_t


def test_engine_guard_accepts_5m_and_rejects_1m():
    start = datetime(2026, 8, 11, 1, 0, tzinfo=UTC)
    five = pd.DataFrame(
        [
            {
                "timestamp": start + timedelta(minutes=5 * i),
                "close_time": start + timedelta(minutes=5 * i + 5),
                "open": 1.0,
                "high": 2.0,
                "low": 0.5,
                "close": 1.2,
                "volume": 1.0,
            }
            for i in range(20)
        ]
    )
    guarded = assert_five_minute_frame(five)
    assert len(guarded) == 20
    one = pd.DataFrame(
        [
            {
                "timestamp": start + timedelta(minutes=i),
                "close_time": start + timedelta(minutes=i + 1),
                "open": 1.0,
                "high": 2.0,
                "low": 0.5,
                "close": 1.2,
                "volume": 1.0,
            }
            for i in range(20)
        ]
    )
    with pytest.raises(FiveMinuteFrameError):
        assert_five_minute_frame(one)
    with pytest.raises(FiveMinuteFrameError):
        run_pools_once(one)
    with pytest.raises((FiveMinuteFrameError, Exception)):
        call_plan_orders(one, symbol="X", entry_time=start + timedelta(minutes=30), entry_price=1.0, direction="LONG")


def test_gap_of_10m_is_allowed_not_filled():
    start = datetime(2026, 8, 11, 1, 0, tzinfo=UTC)
    five = pd.DataFrame(
        [
            {
                "timestamp": start,
                "close_time": start + timedelta(minutes=5),
                "open": 1.0,
                "high": 2.0,
                "low": 0.5,
                "close": 1.2,
                "volume": 1.0,
            },
            {
                "timestamp": start + timedelta(minutes=10),
                "close_time": start + timedelta(minutes=15),
                "open": 1.0,
                "high": 2.0,
                "low": 0.5,
                "close": 1.2,
                "volume": 1.0,
            },
        ]
    )
    guarded = assert_five_minute_frame(five)
    assert len(guarded) == 2


def test_signal_tf_does_not_change_pool_interval():
    stamp = pool_pipeline_stamp()
    assert stamp["source_interval"] == SOURCE_INTERVAL == "1m"
    assert stamp["pool_interval"] == POOL_INTERVAL == "5m"
    assert stamp["aggregation"] == "strict_contiguous_1m_to_5m"
    assert stamp["replay"] is False
    html = (DASHBOARD / "templates" / "stoch_signale.html").read_text(encoding="utf-8")
    assert 'value="wave_fade_no_be50_v1" selected' in html
    js = (DASHBOARD / "static" / "js" / "stoch_signale.js").read_text(encoding="utf-8")
    assert "Signal-TF" in js
    assert "Pool-TF" in js
    assert "Source: ClickHouse 1m" in html
    assert "Pool calculation: closed 5m candles" in html


def test_batch_uses_single_pass_not_call_plan_orders():
    src = (DASHBOARD / "pool_order_plan_v1" / "batch.py").read_text(encoding="utf-8")
    assert "run_pools_once" in src
    assert "call_plan_orders" not in src
    assert "pool_pipeline_stamp" in src


def test_engine_counter_increments_once_per_call():
    start = datetime(2026, 8, 11, 1, 0, tzinfo=UTC)
    five = pd.DataFrame(
        [
            {
                "timestamp": start + timedelta(minutes=5 * i),
                "close_time": start + timedelta(minutes=5 * i + 5),
                "open": 1.0,
                "high": 2.0,
                "low": 0.5,
                "close": 1.2,
                "volume": 1.0,
            }
            for i in range(30)
        ]
    )
    reset_pool_engine_run_count()
    from pool_order_plan_v1.pool_snapshot import pool_engine_run_count

    before = pool_engine_run_count()
    run_pools_once(five)
    assert pool_engine_run_count() - before == 1


def test_ace_run_recognized_as_5m_and_close_derived(monkeypatch):
    import json

    manifest = json.loads(ACE_MANIFEST.read_text(encoding="utf-8"))
    assert is_confirmed_5m_pool_run(manifest)
    assert manifest.get("interval") == "1m"
    assert manifest.get("pool_interval") is None  # frozen file untouched
    monkeypatch.setenv("ENABLE_POOL_ORDER_PLAN_V1", "true")
    payload = research_signals_response()
    assert payload["source_interval"] == "1m"
    assert payload["pool_interval"] == "5m"
    assert payload["aggregation"] == "strict_contiguous_1m_to_5m"
    row = payload["signals"][0]
    assert row["pool_interval"] == "5m"
    assert row["source_interval"] == "1m"
    assert row["signal_timeframe"] in ("15m", "30m", "1h", "4h")
    assert row["pool_timeframe"] == "5m"
    assert row["signal_timeframe"] != row["pool_timeframe"]
    assert row["last_5m_open"]
    assert row["last_5m_close"]
    assert row["last_5m_close_derived"] is True
    assert last_5m_close_from_open(row["last_5m_open"]).strftime("%Y-%m-%dT%H:%M:%SZ") == row["last_5m_close"]
    raw = ACE_MANIFEST.read_text(encoding="utf-8")
    assert '"pool_interval"' not in raw


def test_no_dashboard_process_control_in_feed():
    feed = (DASHBOARD / "pool_order_plan_v1" / "research_feed.py").read_text(encoding="utf-8")
    assert "systemctl" not in feed
    assert "os.kill" not in feed

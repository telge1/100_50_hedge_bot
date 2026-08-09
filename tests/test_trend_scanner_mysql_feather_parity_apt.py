"""Unit tests for MySQL↔Feather parity smoke helpers."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from orderbook_analyse.c3_protected_low_historical_catalog import rising_edge_mask
from orderbook_analyse.trend_scanner_multitimeframe import aggregate_ohlcv_from_5m
from orderbook_analyse.trend_scanner_mysql_feather_parity.compare import (
    compare_ohlcv,
    match_break_events,
)
from orderbook_analyse.trend_scanner_mysql_feather_parity.load import (
    clip_ohlcv,
    comparison_window,
    mysql_quality_checks,
)


def _5m(start: str, n: int, *, px0: float = 1.0) -> pd.DataFrame:
    t0 = pd.Timestamp(start, tz="UTC")
    rows = []
    for i in range(n):
        ts = t0 + pd.Timedelta(minutes=5 * i)
        px = px0 + i * 0.0001
        rows.append(
            {
                "timestamp": ts,
                "open": px,
                "high": px + 0.001,
                "low": px - 0.001,
                "close": px + 0.0005,
                "volume": 100.0 + i,
                "close_time": ts + pd.Timedelta(minutes=5),
            }
        )
    return pd.DataFrame(rows)


def test_mysql_quality_schema_and_utc():
    df = _5m("2026-01-01 00:00:00", 12)
    q = mysql_quality_checks(df)
    assert q["n"] == 12
    assert q["n_gaps_5m"] == 0
    assert q["n_duplicate_timestamps"] == 0
    assert q["close_time_equals_open_plus_5m"] is True
    assert q["sorted_ascending"] is True


def test_comparison_window():
    a = _5m("2026-01-01 00:00:00", 100)
    b = _5m("2026-01-01 01:00:00", 80)  # starts later, ends earlier-ish
    # b: 01:00 .. 01:00+79*5m
    win = comparison_window(a, b)
    assert win["comparison_start"] == pd.Timestamp("2026-01-01 01:00:00", tz="UTC")
    assert win["comparison_end"] == pd.to_datetime(b["timestamp"], utc=True).iloc[-1]
    clipped = clip_ohlcv(a, start=win["comparison_start"], end=win["comparison_end"])
    assert clipped["timestamp"].iloc[0] == win["comparison_start"]
    assert clipped["timestamp"].iloc[-1] <= win["comparison_end"]


def test_complete_1h_4h_aggregation():
    # 48 * 5m = one complete 4h + four 1h
    df = _5m("2026-01-01 00:00:00", 48)
    h1 = aggregate_ohlcv_from_5m(df, "1h", require_complete=True)
    h4 = aggregate_ohlcv_from_5m(df, "4h", require_complete=True)
    assert len(h1) == 4
    assert len(h4) == 1
    assert bool(h1["complete"].all())
    assert int(h4["n_underlying_5m"].iloc[0]) == 48
    # available_at = open + TF
    assert h1["available_at"].iloc[0] == h1["timestamp"].iloc[0] + pd.Timedelta(hours=1)
    assert h4["available_at"].iloc[0] == h4["timestamp"].iloc[0] + pd.Timedelta(hours=4)


def test_incomplete_bucket_dropped_no_future():
    # 11 bars → incomplete first hour dropped
    df = _5m("2026-01-01 00:00:00", 11)
    h1 = aggregate_ohlcv_from_5m(df, "1h", require_complete=True)
    assert h1.empty


def test_rising_edge_dedup():
    s = pd.Series([False, True, True, True, False, True])
    edge = rising_edge_mask(s)
    assert list(edge.astype(int)) == [0, 1, 0, 0, 0, 1]


def test_ohlcv_compare_detects_mismatch():
    a = _5m("2026-01-01 00:00:00", 5)
    b = a.copy()
    b.loc[2, "close"] = float(b.loc[2, "close"]) + 0.01
    out = compare_ohlcv(a, b)
    assert out["close_mismatch"] == 1
    assert out["raw_ok"] is False


def test_event_match_exact():
    mysql = pd.DataFrame(
        [
            {
                "timeframe": "1h",
                "side": "PH_break",
                "available_at": "2026-01-02T01:00:00Z",
                "level": 1.23,
                "candle_open_ts": "2026-01-02T00:00:00Z",
                "choch": True,
                "in_warmup": False,
            }
        ]
    )
    feather = mysql.copy()
    parity, stats = match_break_events(mysql, feather)
    assert stats["counts"]["EXACT_MATCH"] == 1
    assert parity.iloc[0]["status"] == "EXACT_MATCH"


def test_timestamp_is_open_not_close():
    df = _5m("2026-01-01 00:00:00", 1)
    # open 00:00, close_time 00:05 — scanner timestamp must be open
    assert df["timestamp"].iloc[0] == pd.Timestamp("2026-01-01 00:00:00", tz="UTC")
    assert df["close_time"].iloc[0] == pd.Timestamp("2026-01-01 00:05:00", tz="UTC")

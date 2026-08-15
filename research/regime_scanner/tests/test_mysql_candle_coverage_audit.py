"""Unit tests for MySQL candle coverage audit helpers (no DB writes)."""

from __future__ import annotations

import pandas as pd
import pytest

from research.regime_scanner.mysql_candle_coverage_audit import (
    candle_close_from_open,
    ensure_utc,
    expected_row_count,
    find_duplicate_opens,
    find_gaps,
    invalid_ohlcv_mask,
    normalize_symbol_lookup,
    select_last_closed_candle,
    timeframe_to_seconds,
)


def test_timeframe_to_seconds():
    assert timeframe_to_seconds("5m") == 300
    assert timeframe_to_seconds("15m") == 900
    assert timeframe_to_seconds("1h") == 3600
    assert timeframe_to_seconds("4h") == 14400
    with pytest.raises(ValueError):
        timeframe_to_seconds("weird")


def test_candle_close_from_open():
    o = "2026-07-31T12:00:00Z"
    c = candle_close_from_open(o, "5m")
    assert c == ensure_utc("2026-07-31T12:05:00Z")


def test_utc_naive_localized():
    ts = ensure_utc("2026-07-31 12:00:00")
    assert str(ts.tzinfo) in ("UTC", "UTC+00:00") or ts.tzinfo is not None
    assert ts.hour == 12


def test_expected_row_count_inclusive():
    # 00:00, 00:05, 00:10 => 3
    n = expected_row_count("2026-01-01T00:00:00Z", "2026-01-01T00:10:00Z", "5m")
    assert n == 3


def test_gap_detection():
    opens = [
        ensure_utc("2026-01-01T00:00:00Z"),
        ensure_utc("2026-01-01T00:05:00Z"),
        ensure_utc("2026-01-01T00:20:00Z"),  # missing 00:10 and 00:15
    ]
    gaps = find_gaps(opens, "5m")
    assert len(gaps) == 1
    assert gaps[0]["missing_intervals"] == 2


def test_duplicate_opens():
    opens = [
        ensure_utc("2026-01-01T00:00:00Z"),
        ensure_utc("2026-01-01T00:00:00Z"),
        ensure_utc("2026-01-01T00:05:00Z"),
    ]
    assert find_duplicate_opens(opens) == 1


def test_invalid_ohlcv():
    df = pd.DataFrame(
        {
            "open": [1.0, 2.0, -1.0],
            "high": [2.0, 1.0, 1.0],  # row1 high < low
            "low": [0.5, 1.5, 0.5],
            "close": [1.5, 1.2, 0.8],
            "volume": [1.0, 1.0, 1.0],
        }
    )
    m = invalid_ohlcv_mask(df)
    assert bool(m.iloc[1]) and bool(m.iloc[2])
    assert not bool(m.iloc[0])


def test_no_running_candle_selected():
    opens = [
        ensure_utc("2026-07-31T12:00:00Z"),
        ensure_utc("2026-07-31T12:05:00Z"),
    ]
    closes = [
        ensure_utc("2026-07-31T12:05:00Z"),
        ensure_utc("2026-07-31T12:10:00Z"),
    ]
    # query inside second candle [12:05, 12:10)
    sel = select_last_closed_candle(opens, closes, "2026-07-31T12:07:00Z")
    assert sel["selected_last_candle_open_utc"] == ensure_utc("2026-07-31T12:00:00Z").isoformat()
    assert sel["causality_pass"] is True

    # exact close of first
    sel2 = select_last_closed_candle(opens, closes, "2026-07-31T12:05:00Z")
    assert sel2["selected_last_candle_open_utc"] == ensure_utc("2026-07-31T12:00:00Z").isoformat()

    # one second before close of first -> no candle yet from first? close 12:05, query 12:04:59 -> none from first
    sel3 = select_last_closed_candle(opens, closes, "2026-07-31T12:04:59Z")
    assert sel3["selected_last_candle_open_utc"] is None

    # before data
    sel4 = select_last_closed_candle(opens, closes, "2026-07-30T00:00:00Z")
    assert sel4["selected_last_candle_open_utc"] is None

    # after data
    sel5 = select_last_closed_candle(opens, closes, "2026-08-01T00:00:00Z")
    assert sel5["selected_last_candle_open_utc"] == ensure_utc("2026-07-31T12:05:00Z").isoformat()


def test_symbol_alias_normalization():
    avail = ["APTUSDT", "DOGEUSDT"]
    assert normalize_symbol_lookup("APT/USDT", avail) == "APTUSDT"
    assert normalize_symbol_lookup("APT_USDT", avail) == "APTUSDT"
    assert normalize_symbol_lookup("UNKNOWN", avail) is None

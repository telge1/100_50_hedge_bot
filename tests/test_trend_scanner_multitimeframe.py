"""Unit tests for multi-timeframe structure aggregation and adapter TF param."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from orderbook_analyse.c3_protected_low_historical_catalog import rising_edge_mask
from orderbook_analyse.trend_scanner_adapter import run_c34b_structure
from orderbook_analyse.trend_scanner_multitimeframe import (
    aggregate_ohlcv_from_5m,
    asof_attach_htf,
)


def _synth_5m(n: int, start: datetime | None = None) -> pd.DataFrame:
    start = start or datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    rows = []
    price = 100.0
    for i in range(n):
        ts = start + timedelta(minutes=5 * i)
        o = price
        c = price + (0.1 if i % 2 == 0 else -0.05)
        rows.append(
            {
                "timestamp": ts,
                "open": o,
                "high": max(o, c) + 0.2,
                "low": min(o, c) - 0.2,
                "close": c,
                "volume": 1.0 + i * 0.01,
            }
        )
        price = c
    return pd.DataFrame(rows)


def test_aggregate_1h_needs_12_bars_incomplete_dropped() -> None:
    # 11 bars → incomplete hour dropped
    df = _synth_5m(11)
    agg = aggregate_ohlcv_from_5m(df, "1h", require_complete=True)
    assert len(agg) == 0

    # 12 bars → one complete hour
    df12 = _synth_5m(12)
    agg12 = aggregate_ohlcv_from_5m(df12, "1h", require_complete=True)
    assert len(agg12) == 1
    assert bool(agg12.iloc[0]["complete"])
    assert int(agg12.iloc[0]["n_underlying_5m"]) == 12
    assert agg12.iloc[0]["available_at"] == agg12.iloc[0]["timestamp"] + pd.Timedelta(hours=1)

    # 13 bars → still one complete hour (last incomplete dropped)
    df13 = _synth_5m(13)
    agg13 = aggregate_ohlcv_from_5m(df13, "1h", require_complete=True)
    assert len(agg13) == 1


def test_aggregate_4h_utc_anchors() -> None:
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    df = _synth_5m(48 * 3, start=start)  # three full 4h blocks
    agg = aggregate_ohlcv_from_5m(df, "4h", require_complete=True)
    assert len(agg) == 3
    hours = [int(pd.Timestamp(t).hour) for t in agg["timestamp"]]
    assert hours == [0, 4, 8]
    for h in hours:
        assert h in {0, 4, 8, 12, 16, 20}


def test_rising_edge_dedup() -> None:
    s = pd.Series([False, True, True, False, True])
    m = rising_edge_mask(s)
    assert list(m) == [False, True, False, False, True]
    assert int(m.sum()) == 2


def test_asof_join_no_lookahead_synthetic() -> None:
    base = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)
    five = pd.DataFrame(
        {
            "available_at": [base + timedelta(minutes=5 * i) for i in range(1, 7)],
            "close": [100.0 + i for i in range(6)],
            "protected_low": [90.0] * 6,
            "protected_high": [110.0] * 6,
        }
    )
    # 1h structure available only at 02:00
    htf = pd.DataFrame(
        {
            "available_at": [datetime(2026, 1, 1, 2, 0, tzinfo=timezone.utc)],
            "protected_low": [95.0],
            "protected_high": [105.0],
            "trend_segment_id": ["s1"],
            "close_break_protected_down": [False],
            "close_break_protected_up": [False],
            "major_direction": [-1],
        }
    )
    joined = asof_attach_htf(five, htf, suffix="1h")
    # Bars before 02:00 must not see HTF level
    early = joined[joined["available_at"] < datetime(2026, 1, 1, 2, 0, tzinfo=timezone.utc)]
    assert early["protected_low_1h"].isna().all()
    late = joined[joined["available_at"] >= datetime(2026, 1, 1, 2, 0, tzinfo=timezone.utc)]
    assert (late["protected_low_1h"] == 95.0).all()
    # No future HTF
    mask = late["available_at_1h"].notna()
    assert (late.loc[mask, "available_at_1h"] <= late.loc[mask, "available_at"]).all()


def test_run_c34b_structure_timeframe_1h_available_at() -> None:
    df = _synth_5m(80)
    h1 = aggregate_ohlcv_from_5m(df, "1h", require_complete=True)
    assert len(h1) >= 5
    struct = run_c34b_structure(h1[["timestamp", "open", "high", "low", "close", "volume"]], timeframe="1h")
    assert (struct["timeframe"] == "1h").all()
    delta = pd.to_datetime(struct["available_at"], utc=True) - pd.to_datetime(
        struct["candle_open_ts"], utc=True
    )
    assert (delta == pd.Timedelta(hours=1)).all()


def test_run_c34b_structure_5m_default_available_at() -> None:
    df = _synth_5m(40)
    struct = run_c34b_structure(df)
    assert (struct["timeframe"] == "5m").all()
    delta = pd.to_datetime(struct["available_at"], utc=True) - pd.to_datetime(
        struct["candle_open_ts"], utc=True
    )
    assert (delta == pd.Timedelta(minutes=5)).all()

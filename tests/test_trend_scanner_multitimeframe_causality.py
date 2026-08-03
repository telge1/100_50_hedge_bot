"""Causality tests for multi-timeframe structure scanning."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from orderbook_analyse.trend_scanner_multitimeframe import (
    aggregate_ohlcv_from_5m,
    asof_attach_htf,
    compute_alignment_columns,
    run_structure_for_timeframe,
)


def _synth_5m(n: int, start: datetime | None = None) -> pd.DataFrame:
    start = start or datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    rows = []
    price = 50.0
    for i in range(n):
        ts = start + timedelta(minutes=5 * i)
        c = price + 0.05 * ((i % 5) - 2)
        rows.append(
            {
                "timestamp": ts,
                "open": price,
                "high": max(price, c) + 0.3,
                "low": min(price, c) - 0.3,
                "close": c,
                "volume": 10.0,
            }
        )
        price = c
    return pd.DataFrame(rows)


def test_incomplete_htf_no_structure_row() -> None:
    # 47 five-minute bars → incomplete 4h bucket → dropped
    df = _synth_5m(47)
    agg = aggregate_ohlcv_from_5m(df, "4h", require_complete=True)
    assert len(agg) == 0
    # Without complete candles there is nothing to scan
    assert agg.empty


def test_htf_level_not_visible_before_available_at() -> None:
    df = _synth_5m(12 * 6)  # six hours
    h1 = aggregate_ohlcv_from_5m(df, "1h", require_complete=True)
    assert len(h1) >= 3
    struct_1h = run_structure_for_timeframe(h1, timeframe="1h", symbol="TEST", warmup_bars=2)
    five = aggregate_ohlcv_from_5m(df, "5m", require_complete=True)
    struct_5m = run_structure_for_timeframe(five, timeframe="5m", symbol="TEST", warmup_bars=2)

    # Pick first 1h row with a protected_low
    with_pl = struct_1h.dropna(subset=["protected_low"])
    if with_pl.empty:
        # Still validate join causality with synthetic level attach
        htf = struct_1h.iloc[[0]].copy()
        htf["protected_low"] = 42.0
        htf["protected_high"] = 99.0
    else:
        htf = with_pl.iloc[[0]].copy()

    known = pd.Timestamp(htf.iloc[0]["available_at"])
    joined = asof_attach_htf(struct_5m, htf, suffix="1h")
    before = joined[joined["available_at"] < known]
    assert before["protected_low_1h"].isna().all()
    at_or_after = joined[joined["available_at"] >= known]
    assert at_or_after["protected_low_1h"].notna().any()


def test_5m_break_does_not_mutate_1h_structure_frame() -> None:
    df = _synth_5m(12 * 8)
    h1 = aggregate_ohlcv_from_5m(df, "1h", require_complete=True)
    struct_1h = run_structure_for_timeframe(h1, timeframe="1h", symbol="TEST", warmup_bars=2)
    before = struct_1h.copy(deep=True)

    five = aggregate_ohlcv_from_5m(df, "5m", require_complete=True)
    struct_5m = run_structure_for_timeframe(five, timeframe="5m", symbol="TEST", warmup_bars=2)
    # Force a 5m break flag (mutate 5m only)
    struct_5m = struct_5m.copy()
    struct_5m.loc[struct_5m.index[:5], "close_break_protected_down"] = True

    joined = asof_attach_htf(struct_5m, struct_1h, suffix="1h")
    joined = compute_alignment_columns(joined)

    # Original 1h frame unchanged
    pd.testing.assert_frame_equal(before, struct_1h)
    # Joined 5m may show pl_break_5m without changing 1h source
    assert "pl_break_5m" in joined.columns
    assert before["close_break_protected_down"].equals(struct_1h["close_break_protected_down"])

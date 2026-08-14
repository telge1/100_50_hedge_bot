from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from pool_order_plan_v1.config import FEE_PCT
from pool_order_plan_v1.partial_exits import first_outcome_open, simulate_partial_exits


UTC = timezone.utc


def _c1m(start: datetime, n: int, high: float, low: float, close: float | None = None) -> pd.DataFrame:
    rows = []
    for i in range(n):
        ts = start + timedelta(minutes=i)
        rows.append(
            {
                "timestamp": pd.Timestamp(ts),
                "open": 100.0,
                "high": high,
                "low": low,
                "close": close if close is not None else 100.0,
                "volume": 1.0,
            }
        )
    return pd.DataFrame(rows)


def test_first_outcome_minute_and_subminute():
    assert first_outcome_open(datetime(2026, 8, 11, 1, 17, tzinfo=UTC)) == datetime(
        2026, 8, 11, 1, 17, tzinfo=UTC
    )
    assert first_outcome_open(datetime(2026, 8, 11, 1, 17, 30, tzinfo=UTC)) == datetime(
        2026, 8, 11, 1, 18, tzinfo=UTC
    )


def test_long_tp1_tp2():
    start = datetime(2026, 8, 11, 1, 17, tzinfo=UTC)
    df = _c1m(start, 5, high=102.0, low=99.5)
    out = simulate_partial_exits(
        direction="LONG",
        entry_time=start,
        entry_price=100.0,
        sl_price=99.0,
        tp1_price=101.0,
        tp1_size=0.5,
        tp2_price=102.0,
        tp2_size=0.5,
        candles_1m=df,
        timeframe="15m",
    )
    assert out["outcome"] == "TP1_TP2"
    assert abs(out["gross_pnl_pct"] - (0.5 * 1.0 + 0.5 * 2.0)) < 1e-9
    assert abs(out["fees_pct"] - FEE_PCT) < 1e-9


def test_long_tp1_then_sl():
    start = datetime(2026, 8, 11, 1, 17, tzinfo=UTC)
    rows = []
    rows.append({"timestamp": pd.Timestamp(start), "open": 100, "high": 101.5, "low": 99.8, "close": 101, "volume": 1})
    rows.append(
        {
            "timestamp": pd.Timestamp(start + timedelta(minutes=1)),
            "open": 101,
            "high": 101.2,
            "low": 98.5,
            "close": 99,
            "volume": 1,
        }
    )
    df = pd.DataFrame(rows)
    out = simulate_partial_exits(
        direction="LONG",
        entry_time=start,
        entry_price=100.0,
        sl_price=99.0,
        tp1_price=101.0,
        tp1_size=0.5,
        tp2_price=103.0,
        tp2_size=0.5,
        candles_1m=df,
        timeframe="15m",
    )
    assert out["outcome"] == "TP1_SL"
    expected_gross = 0.5 * 1.0 + 0.5 * (-1.0)
    assert abs(out["gross_pnl_pct"] - expected_gross) < 1e-9


def test_short_formula_and_tp1_tp2():
    start = datetime(2026, 8, 11, 1, 17, tzinfo=UTC)
    df = _c1m(start, 3, high=100.2, low=97.0)
    out = simulate_partial_exits(
        direction="SHORT",
        entry_time=start,
        entry_price=100.0,
        sl_price=101.0,
        tp1_price=99.0,
        tp1_size=0.5,
        tp2_price=98.0,
        tp2_size=0.5,
        candles_1m=df,
        timeframe="15m",
    )
    assert out["outcome"] == "TP1_TP2"
    # (100-99)/100 * 0.5 + (100-98)/100 * 0.5 = 1.5
    assert abs(out["gross_pnl_pct"] - 1.5) < 1e-9
    wrong = 100 / 98 - 1
    assert abs(out["gross_pnl_pct"] / 100.0 - wrong) > 1e-6


def test_same_bar_sl_first_full():
    start = datetime(2026, 8, 11, 1, 17, tzinfo=UTC)
    df = _c1m(start, 1, high=102.0, low=98.0)
    out = simulate_partial_exits(
        direction="LONG",
        entry_time=start,
        entry_price=100.0,
        sl_price=99.0,
        tp1_price=101.0,
        tp1_size=0.5,
        tp2_price=102.0,
        tp2_size=0.5,
        candles_1m=df,
        timeframe="15m",
    )
    assert out["outcome"] == "SL"
    assert out["sl_first"] is True
    assert abs(out["legs"][0]["size"] - 1.0) < 1e-9


def test_one_target_full_tp1():
    start = datetime(2026, 8, 11, 1, 17, tzinfo=UTC)
    df = _c1m(start, 2, high=101.5, low=99.5)
    out = simulate_partial_exits(
        direction="LONG",
        entry_time=start,
        entry_price=100.0,
        sl_price=99.0,
        tp1_price=101.0,
        tp1_size=1.0,
        tp2_price=None,
        tp2_size=None,
        candles_1m=df,
        timeframe="15m",
    )
    assert out["outcome"] == "TP1"
    assert abs(out["fees_pct"] - FEE_PCT) < 1e-9


def test_subminute_skips_partial_minute_bar():
    entry = datetime(2026, 8, 11, 1, 17, 30, tzinfo=UTC)
    rows = [
        {"timestamp": pd.Timestamp(datetime(2026, 8, 11, 1, 17, tzinfo=UTC)), "open": 100, "high": 105, "low": 100, "close": 105, "volume": 1},
        {"timestamp": pd.Timestamp(datetime(2026, 8, 11, 1, 18, tzinfo=UTC)), "open": 100, "high": 100.2, "low": 99.9, "close": 100, "volume": 1},
    ]
    out = simulate_partial_exits(
        direction="LONG",
        entry_time=entry,
        entry_price=100.0,
        sl_price=90.0,
        tp1_price=104.0,
        tp1_size=1.0,
        tp2_price=None,
        tp2_size=None,
        candles_1m=pd.DataFrame(rows),
        timeframe="15m",
    )
    assert out["outcome"] == "OPEN"

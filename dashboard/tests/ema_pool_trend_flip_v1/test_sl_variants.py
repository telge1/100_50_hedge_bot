from __future__ import annotations

from ema_pool_trend_flip_v1.config import RATCHET_VARIANT, STATIC_VARIANT
from ema_pool_trend_flip_v1.simulate import simulate_path
import pandas as pd
from datetime import datetime, timedelta, timezone

UTC = timezone.utc


def test_static_sl_does_not_move():
    et = datetime(2026, 8, 12, 16, 0, tzinfo=UTC)
    rows = []
    for i in range(30):
        ot = et + timedelta(minutes=i)
        rows.append(
            {
                "open_time": ot,
                "open": 100.0,
                "high": 100.2,
                "low": 99.9,
                "close": 100.0,
            }
        )
    m1 = pd.DataFrame(rows)
    five = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp(et - timedelta(minutes=5)),
                "close_time": pd.Timestamp(et),
                "open": 100.0,
                "high": 100.2,
                "low": 99.8,
                "close": 100.0,
                "volume": 1,
            }
        ]
    )
    tf = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp(et - timedelta(minutes=15 * i)),
                "close_time": pd.Timestamp(et - timedelta(minutes=15 * (i - 1) if i else 0)),
                "open": 100.0,
                "high": 100.2,
                "low": 99.8,
                "close": 100.0,
                "volume": 1,
            }
            for i in range(30, 0, -1)
        ]
    )
    out = simulate_path(
        executed_direction="LONG",
        entry_time=et,
        entry_price=100.0,
        initial_sl=98.0,
        one_minute=m1,
        five_minute=five,
        all_pools=[],
        signal_tf_bars=tf,
        variant=STATIC_VARIANT,
        window_end=et + timedelta(hours=1),
        ema_exit_kind="CONFIRMED_STRONG_BEARISH_EMA_CROSS",
    )
    assert out["sl_price_final"] == 98.0
    assert out["ratchet_steps"] == []


def test_ratchet_never_lowers_long_sl():
    # empty pools => no ratchet improvement
    et = datetime(2026, 8, 12, 16, 0, tzinfo=UTC)
    rows = []
    for i in range(20):
        ot = et + timedelta(minutes=i)
        rows.append({"open_time": ot, "open": 100.0, "high": 100.1, "low": 99.95, "close": 100.0})
    m1 = pd.DataFrame(rows)
    five = pd.DataFrame(
        columns=["timestamp", "close_time", "open", "high", "low", "close", "volume"]
    )
    tf = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp(et - timedelta(minutes=15)),
                "close_time": pd.Timestamp(et),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 1,
            }
        ]
    )
    out = simulate_path(
        executed_direction="LONG",
        entry_time=et,
        entry_price=100.0,
        initial_sl=98.5,
        one_minute=m1,
        five_minute=five,
        all_pools=[],
        signal_tf_bars=tf,
        variant=RATCHET_VARIANT,
        window_end=et + timedelta(minutes=25),
        ema_exit_kind="CONFIRMED_STRONG_BEARISH_EMA_CROSS",
    )
    assert out["sl_price_final"] >= 98.5
    assert all(s["sl_price"] >= 98.5 for s in out["ratchet_steps"])

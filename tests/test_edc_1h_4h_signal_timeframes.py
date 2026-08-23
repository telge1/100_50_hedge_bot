"""Tests for XRP 1h/4h signal timeframe research runner."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from orderbook_analyse.cluster_sweep_research.clickhouse_source import (
    _timeframe_minutes,
    aggregate_timeframe,
)
from orderbook_analyse.ema_dual_cross_multisource.timeframes import bar_close, timeframe_duration
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.tpsl_pnl_engine import (
    apply_costs,
    simulate_tpsl_trade,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.xrp_30d_1h_4h_signal_timeframes_runner import (
    HORIZONS_BY_TF,
    WARMUP_MIN_BARS,
    WARMUP_PREFERRED_BARS,
    WINDOW_END,
    WINDOW_START,
    _first_1m_open_after,
    _feature_audit,
    _warmup_audit,
)


def test_timeframe_minutes_hours():
    assert _timeframe_minutes("5m") == 5
    assert _timeframe_minutes("15m") == 15
    assert _timeframe_minutes("30m") == 30
    assert _timeframe_minutes("1h") == 60
    assert _timeframe_minutes("4h") == 240


def test_aggregate_timeframe_1h_4h_parity_with_minutes():
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(600):
        t = start + timedelta(minutes=i)
        px = 1.0 + (i % 17) * 0.001
        rows.append({"open_time": t.replace(tzinfo=None), "open": px, "high": px + 0.001, "low": px - 0.001, "close": px, "volume": 1.0})
    c1m = pd.DataFrame(rows)
    h1 = aggregate_timeframe(c1m, "1h")
    h4 = aggregate_timeframe(c1m, "4h")
    m15 = aggregate_timeframe(c1m, "15m")
    assert len(h1) >= 9
    assert len(h4) >= 2
    assert len(m15) >= 30
    # 1h bars should be 60 minutes apart
    t0 = pd.Timestamp(h1.iloc[0]["open_time"])
    t1 = pd.Timestamp(h1.iloc[1]["open_time"])
    assert (t1 - t0).total_seconds() == 3600


def test_decision_at_1h_4h():
    open_1h = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    open_4h = datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc)
    assert bar_close(open_1h, "1h") == open_1h + timedelta(hours=1)
    assert bar_close(open_4h, "4h") == open_4h + timedelta(hours=4)
    assert timeframe_duration("1h") == timedelta(hours=1)
    assert timeframe_duration("4h") == timedelta(hours=4)


def test_entry_first_1m_at_or_after_decision():
    start = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(10):
        t = start + timedelta(minutes=i)
        rows.append(
            {
                "open_time": t.replace(tzinfo=None),
                "open": 1.0 + i * 0.01,
                "high": 1.02,
                "low": 0.99,
                "close": 1.0,
                "volume": 1.0,
            }
        )
    c1m = pd.DataFrame(rows)
    decision = start + timedelta(minutes=3)
    entry_at, entry_px = _first_1m_open_after(c1m, decision)
    assert entry_at == decision
    assert abs(entry_px - 1.03) < 1e-9
    # no candle before decision
    assert entry_at >= decision


def test_no_candle_before_entry_in_tpsl_path():
    start = datetime(2026, 7, 24, 0, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(120):
        t = start + timedelta(minutes=i)
        # rising then falling
        px = 1.0 + min(i, 30) * 0.001
        rows.append(
            {
                "open_time": t.replace(tzinfo=None),
                "open": px,
                "high": px + 0.002,
                "low": px - 0.002,
                "close": px,
                "volume": 1.0,
            }
        )
    c1m = pd.DataFrame(rows)
    entry = start + timedelta(minutes=5)
    sim = simulate_tpsl_trade(
        c1m, direction="BULLISH", entry_at=entry, entry_price=1.005, tp_pct=0.4, sl_pct=0.5, horizon_min=60
    )
    assert sim["exit_at"] is not None
    assert datetime.fromisoformat(sim["exit_at"]) >= entry


def test_same_bar_sl_first():
    t0 = datetime(2026, 7, 24, 0, 0, tzinfo=timezone.utc)
    rows = [
        {
            "open_time": t0.replace(tzinfo=None),
            "open": 1.0,
            "high": 1.01,
            "low": 0.994,
            "close": 1.0,
            "volume": 1.0,
        }
    ]
    c1m = pd.DataFrame(rows)
    sim = simulate_tpsl_trade(
        c1m, direction="BULLISH", entry_at=t0, entry_price=1.0, tp_pct=0.4, sl_pct=0.5, horizon_min=60
    )
    assert sim["exit_reason"] == "SL_EXIT"
    assert sim["same_bar_conflict"] is True


def test_costs_once():
    trade = {"gross_return_pct": 0.4}
    paid = apply_costs(trade, 0.15)
    assert paid["net_return_pct"] == pytest.approx(0.25)
    assert paid["costs_usdt"] == pytest.approx(1.5)
    assert paid["gross_pnl_usdt"] == pytest.approx(4.0)
    assert paid["net_pnl_usdt"] == pytest.approx(2.5)


def test_long_short_symmetry_levels():
    t0 = datetime(2026, 7, 24, 0, 0, tzinfo=timezone.utc)
    # long: high hits TP
    rows_long = [
        {"open_time": t0.replace(tzinfo=None), "open": 1.0, "high": 1.005, "low": 0.999, "close": 1.002, "volume": 1.0}
    ]
    # short mirrored
    rows_short = [
        {"open_time": t0.replace(tzinfo=None), "open": 1.0, "high": 1.001, "low": 0.995, "close": 0.998, "volume": 1.0}
    ]
    long = simulate_tpsl_trade(
        pd.DataFrame(rows_long), direction="BULLISH", entry_at=t0, entry_price=1.0, tp_pct=0.4, sl_pct=0.5, horizon_min=60
    )
    short = simulate_tpsl_trade(
        pd.DataFrame(rows_short), direction="BEARISH", entry_at=t0, entry_price=1.0, tp_pct=0.4, sl_pct=0.5, horizon_min=60
    )
    assert long["exit_reason"] == "TP_EXIT"
    assert short["exit_reason"] == "TP_EXIT"
    assert long["gross_return_pct"] == pytest.approx(short["gross_return_pct"])


def test_horizons_1h_4h_configured():
    assert [h for h, _ in HORIZONS_BY_TF["1h"]] == ["1h", "2h", "4h", "8h", "12h"]
    assert [h for h, _ in HORIZONS_BY_TF["4h"]] == ["4h", "8h", "12h", "24h"]
    assert WINDOW_START.isoformat().startswith("2026-07-24")
    assert WINDOW_END.isoformat().startswith("2026-08-23")
    assert WARMUP_PREFERRED_BARS >= WARMUP_MIN_BARS >= 250


def test_feature_audit_timeframe_generic():
    audit = _feature_audit()
    assert audit["hardcoded_5m_15m_30m_in_feature_builder"] is False
    assert audit["aggregate_timeframe_supports_hours"] is True


def test_warmup_audit_insufficient():
    # empty pre-window
    df = pd.DataFrame(
        {
            "open_time": [datetime(2026, 7, 24, 1, 0)],
            "open": [1.0],
            "high": [1.0],
            "low": [1.0],
            "close": [1.0],
            "volume": [1.0],
            "ema_59": [1.0],
        }
    )
    class Cfg:
        ema_slow = 59

    wa = _warmup_audit(df, timeframe="1h", start_at=WINDOW_START, cfg=Cfg())
    assert wa["status"] == "INSUFFICIENT"

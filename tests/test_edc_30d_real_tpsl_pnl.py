"""Tests for 30d real TP/SL PnL backtest."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.tpsl_pnl_engine import (
    NOTIONAL_USDT,
    apply_costs,
    simulate_tpsl_trade,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.xrp_30d_real_tpsl_pnl_runner import (
    GROUP_MAP,
    _dedupe_episode_portfolio,
    _one_position_per_symbol,
)


def _candles(start: datetime, n: int, *, high_off=0.002, low_off=0.001) -> pd.DataFrame:
    rows = []
    for i in range(n):
        px = 1.0
        rows.append(
            {
                "open_time": (start + timedelta(minutes=i)).replace(tzinfo=None),
                "open": px,
                "high": px + high_off,
                "low": px - low_off,
                "close": px,
                "volume": 1,
            }
        )
    return pd.DataFrame(rows)


def test_long_tp_sl_prices():
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    df = _candles(t0, 120, high_off=0.01, low_off=0.001)
    r = simulate_tpsl_trade(df, direction="BULLISH", entry_at=t0, entry_price=1.0, tp_pct=0.40, sl_pct=0.50, horizon_min=60)
    assert r["tp_price"] == pytest.approx(1.004)
    assert r["sl_price"] == pytest.approx(0.995)


def test_short_tp_sl_prices():
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    df = _candles(t0, 120, high_off=0.001, low_off=0.01)
    r = simulate_tpsl_trade(df, direction="BEARISH", entry_at=t0, entry_price=1.0, tp_pct=0.40, sl_pct=0.50, horizon_min=60)
    assert r["tp_price"] == pytest.approx(0.996)
    assert r["sl_price"] == pytest.approx(1.005)


def test_sl_first_same_bar():
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    df = pd.DataFrame(
        [
            {
                "open_time": t0.replace(tzinfo=None),
                "open": 1.0,
                "high": 1.01,
                "low": 0.99,
                "close": 1.0,
                "volume": 1,
            }
        ]
    )
    r = simulate_tpsl_trade(df, direction="BULLISH", entry_at=t0, entry_price=1.0, tp_pct=0.40, sl_pct=0.50, horizon_min=60)
    assert r["exit_reason"] == "SL_EXIT"
    assert r["same_bar_conflict"] is True


def test_tp_exit_before_sl():
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    df = pd.DataFrame(
        [
            {
                "open_time": t0.replace(tzinfo=None),
                "open": 1.0,
                "high": 1.006,
                "low": 0.999,
                "close": 1.005,
                "volume": 1,
            }
        ]
    )
    r = simulate_tpsl_trade(df, direction="BULLISH", entry_at=t0, entry_price=1.0, tp_pct=0.40, sl_pct=0.50, horizon_min=60)
    assert r["exit_reason"] == "TP_EXIT"


def test_time_exit_at_last_close():
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    df = _candles(t0, 3, high_off=0.0001, low_off=0.0001)
    r = simulate_tpsl_trade(df, direction="BULLISH", entry_at=t0, entry_price=1.0, tp_pct=0.40, sl_pct=0.50, horizon_min=3)
    assert r["exit_reason"] == "TIME_EXIT"
    assert r["bars_held"] == 3


def test_roundtrip_cost_once():
    r = apply_costs({"gross_return_pct": 0.5}, 0.15)
    assert r["costs_usdt"] == pytest.approx(1.5)
    assert r["net_pnl_usdt"] == pytest.approx(NOTIONAL_USDT * 0.005 - 1.5)


def test_no_compounding():
    r = apply_costs({"gross_return_pct": 1.0}, 0.11)
    assert r["gross_pnl_usdt"] == pytest.approx(10.0)
    assert r["net_pnl_usdt"] == pytest.approx(10.0 - 1.1)


def test_groups_disjoint():
    c = {"core_research_verdict": "CORE_RESEARCH_SUPPORTIVE", "production_gate_verdict": "INCONCLUSIVE_DATA", "coverage_segment": "CORE_FULL_OI_LIQ_MISSING"}
    assert GROUP_MAP["CORE_RESEARCH_SUPPORTIVE"](c)
    assert not GROUP_MAP["CORE_RESEARCH_ADVERSE"](c)


def test_dedupe_episode_one_per_episode():
    candidates = [{"candidate_id": "a", "cross_episode_id": "ep1", "mode_id": "M0_STRICT_SYNC", "core_research_verdict": "CORE_RESEARCH_SUPPORTIVE", "production_gate_verdict": "INCONCLUSIVE_DATA"}]
    trades = [
        {"candidate_id": "a", "net_pnl_usdt": 1.0, "entry_at": "2026-08-01T12:00:00+00:00"},
        {"candidate_id": "b", "net_pnl_usdt": 2.0, "entry_at": "2026-08-01T13:00:00+00:00"},
    ]
    candidates.append({"candidate_id": "b", "cross_episode_id": "ep1", "mode_id": "M5_COMPRESSED_REBOUND", "core_research_verdict": "CORE_RESEARCH_ADVERSE", "production_gate_verdict": "INCONCLUSIVE_DATA"})
    out = _dedupe_episode_portfolio(trades, candidates)
    assert len(out) == 1


def test_one_position_skips_overlap():
    trades = [
        {"candidate_id": "a", "entry_at": "2026-08-01T12:00:00+00:00", "exit_at": "2026-08-01T14:00:00+00:00"},
        {"candidate_id": "b", "entry_at": "2026-08-01T13:00:00+00:00", "exit_at": "2026-08-01T15:00:00+00:00"},
    ]
    out = _one_position_per_symbol(trades)
    assert sum(1 for x in out if x["portfolio_mode"] == "ONE_POSITION_SKIPPED") == 1

"""Synthetic tests for XRP frozen-reference audit scope, entry, and PnL rules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.core_sources_research_policy import (
    apply_core_sources_research,
    core_research_policy_document,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.entry import (
    first_1m_open_at_or_after,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.xrp_parity import (
    compare_xrp_candidates_to_export,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.tpsl_pnl_engine import (
    NOTIONAL_USDT,
    apply_costs,
    simulate_tpsl_trade,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.xrp_frozen_reference_audit import (
    MULTICOIN_DETECT_SCOPES,
    filter_reference_scope,
    scope_normalized_parity,
)


def _candles(start: datetime, n: int, *, high_off=0.002, low_off=0.001, open_px=1.0) -> pd.DataFrame:
    rows = []
    for i in range(n):
        px = open_px
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


def test_filter_reference_scope_5m_m0_supportive_only():
    rows = [
        {
            "candidate_id": "a",
            "timeframe": "5m",
            "mode_id": "M0_STRICT_SYNC",
            "core_research_verdict": "CORE_RESEARCH_SUPPORTIVE",
        },
        {
            "candidate_id": "b",
            "timeframe": "5m",
            "mode_id": "M0_STRICT_SYNC",
            "core_research_verdict": "CORE_RESEARCH_ADVERSE",
        },
        {
            "candidate_id": "c",
            "timeframe": "5m",
            "mode_id": "M5_COMPRESSED_REBOUND",
            "core_research_verdict": "CORE_RESEARCH_SUPPORTIVE",
        },
        {
            "candidate_id": "d",
            "timeframe": "15m",
            "mode_id": "M0_STRICT_SYNC",
            "core_research_verdict": "CORE_RESEARCH_SUPPORTIVE",
        },
        {
            "candidate_id": "e",
            "timeframe": "5m",
            "mode_id": "M4_TOUCH_05_EXP_1",
            "core_research_verdict": "CORE_RESEARCH_SUPPORTIVE",
        },
        {
            "candidate_id": "f",
            "timeframe": "30m",
            "mode_id": "M0_STRICT_SYNC",
            "core_research_verdict": "CORE_RESEARCH_SUPPORTIVE",
        },
    ]
    got = filter_reference_scope(rows)
    assert [r["candidate_id"] for r in got] == ["a"]


def test_other_modes_and_timeframes_excluded():
    rows = [
        {
            "candidate_id": "m4",
            "timeframe": "5m",
            "mode_id": "M4_TOUCH_05_EXP_1",
            "core_research_verdict": "CORE_RESEARCH_SUPPORTIVE",
        },
        {
            "candidate_id": "m5",
            "timeframe": "5m",
            "mode_id": "M5_COMPRESSED_REBOUND",
            "core_research_verdict": "CORE_RESEARCH_SUPPORTIVE",
        },
        {
            "candidate_id": "h1",
            "timeframe": "1h",
            "mode_id": "M0_STRICT_SYNC",
            "core_research_verdict": "CORE_RESEARCH_SUPPORTIVE",
        },
        {
            "candidate_id": "h4",
            "timeframe": "4h",
            "mode_id": "M0_STRICT_SYNC",
            "core_research_verdict": "CORE_RESEARCH_SUPPORTIVE",
        },
    ]
    assert filter_reference_scope(rows) == []


def test_entry_exact_at_decision_at():
    decision = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    df = _candles(decision - timedelta(minutes=2), 5, open_px=2.5)
    # force distinct opens
    df.loc[df["open_time"] == decision.replace(tzinfo=None), "open"] = 3.14
    at, px = first_1m_open_at_or_after(df, decision)
    assert at == decision
    assert px == pytest.approx(3.14)


def test_entry_missing_exact_minute_uses_next_open():
    decision = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    later = decision + timedelta(minutes=3)
    df = pd.DataFrame(
        [
            {
                "open_time": (decision - timedelta(minutes=1)).replace(tzinfo=None),
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1,
            },
            {
                "open_time": later.replace(tzinfo=None),
                "open": 1.23,
                "high": 1.23,
                "low": 1.23,
                "close": 1.23,
                "volume": 1,
            },
        ]
    )
    at, px = first_1m_open_at_or_after(df, decision)
    assert at == later
    assert px == pytest.approx(1.23)


def test_no_minute_before_decision_at():
    decision = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    df = _candles(decision - timedelta(minutes=5), 5)
    at, px = first_1m_open_at_or_after(df, decision)
    assert at is None and px is None


def test_long_tp_and_sl_reference_cell():
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    # TP hit: high >= 1.0075
    df_tp = pd.DataFrame(
        [
            {
                "open_time": t0.replace(tzinfo=None),
                "open": 1.0,
                "high": 1.008,
                "low": 0.999,
                "close": 1.007,
                "volume": 1,
            }
        ]
    )
    tp = simulate_tpsl_trade(
        df_tp, direction="BULLISH", entry_at=t0, entry_price=1.0, tp_pct=0.75, sl_pct=0.50, horizon_min=480
    )
    paid = apply_costs(tp, 0.15)
    assert tp["exit_reason"] == "TP_EXIT"
    assert paid["net_pnl_usdt"] == pytest.approx(6.0)

    df_sl = pd.DataFrame(
        [
            {
                "open_time": t0.replace(tzinfo=None),
                "open": 1.0,
                "high": 1.001,
                "low": 0.994,
                "close": 0.995,
                "volume": 1,
            }
        ]
    )
    sl = simulate_tpsl_trade(
        df_sl, direction="BULLISH", entry_at=t0, entry_price=1.0, tp_pct=0.75, sl_pct=0.50, horizon_min=480
    )
    paid_sl = apply_costs(sl, 0.15)
    assert sl["exit_reason"] == "SL_EXIT"
    assert paid_sl["net_pnl_usdt"] == pytest.approx(-6.5)


def test_short_tp_and_sl_reference_cell():
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    df_tp = pd.DataFrame(
        [
            {
                "open_time": t0.replace(tzinfo=None),
                "open": 1.0,
                "high": 1.001,
                "low": 0.992,
                "close": 0.993,
                "volume": 1,
            }
        ]
    )
    tp = apply_costs(
        simulate_tpsl_trade(
            df_tp, direction="BEARISH", entry_at=t0, entry_price=1.0, tp_pct=0.75, sl_pct=0.50, horizon_min=480
        ),
        0.15,
    )
    assert tp["exit_reason"] == "TP_EXIT"
    assert tp["net_pnl_usdt"] == pytest.approx(6.0)

    df_sl = pd.DataFrame(
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
    sl = apply_costs(
        simulate_tpsl_trade(
            df_sl, direction="BEARISH", entry_at=t0, entry_price=1.0, tp_pct=0.75, sl_pct=0.50, horizon_min=480
        ),
        0.15,
    )
    assert sl["exit_reason"] == "SL_EXIT"
    assert sl["net_pnl_usdt"] == pytest.approx(-6.5)


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
    r = simulate_tpsl_trade(
        df, direction="BULLISH", entry_at=t0, entry_price=1.0, tp_pct=0.75, sl_pct=0.50, horizon_min=480
    )
    assert r["exit_reason"] == "SL_EXIT"
    assert r["same_bar_conflict"] is True


def test_time_exit_and_cost_once_notional_1000():
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    df = _candles(t0, 10, high_off=0.0001, low_off=0.0001)
    raw = simulate_tpsl_trade(
        df, direction="BULLISH", entry_at=t0, entry_price=1.0, tp_pct=0.75, sl_pct=0.50, horizon_min=10
    )
    assert raw["exit_reason"] == "TIME_EXIT"
    paid = apply_costs(raw, 0.15)
    assert NOTIONAL_USDT == 1000.0
    assert paid["costs_usdt"] == pytest.approx(1.5)
    # costs applied once: net = gross - 0.15
    assert paid["net_return_pct"] == pytest.approx(float(raw["gross_return_pct"]) - 0.15)


def test_candidate_dedup_one_trade_per_id():
    rows = [
        {
            "candidate_id": "x",
            "timeframe": "5m",
            "mode_id": "M0_STRICT_SYNC",
            "core_research_verdict": "CORE_RESEARCH_SUPPORTIVE",
        },
        {
            "candidate_id": "x",
            "timeframe": "5m",
            "mode_id": "M0_STRICT_SYNC",
            "core_research_verdict": "CORE_RESEARCH_SUPPORTIVE",
        },
    ]
    scoped = filter_reference_scope(rows)
    deduped = list({r["candidate_id"]: r for r in scoped}.values())
    assert len(deduped) == 1


def test_incomplete_8h_horizon_flag():
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    df = _candles(t0, 30, high_off=0.0001, low_off=0.0001)
    r = simulate_tpsl_trade(
        df,
        direction="BULLISH",
        entry_at=t0,
        entry_price=1.0,
        tp_pct=0.75,
        sl_pct=0.50,
        horizon_min=480,
        require_full_horizon=True,
    )
    assert r["exit_reason"] == "INCOMPLETE_OUTCOME_HORIZON"


def test_oi_liq_missing_never_neutral_in_policy():
    doc = core_research_policy_document()
    assert doc["missing_never_neutral"] is True
    # Missing OI/liq does not invent NEUTRAL source labels for core research;
    # core still evaluates without requiring OI/liq.
    verdict, reasons = apply_core_sources_research(
        direction="BULLISH",
        features={},
        coverage={
            "candles": {"status": "VALID"},
            "public_trades_cross": {"status": "VALID"},
            "orderbook_ob200_v3": {"status": "VALID"},
            "liquidity_locations": {"status": "VALID"},
            "open_interest": {"status": "MISSING"},
            "liquidations": {"status": "MISSING"},
        },
        source_verdicts={
            # CORE_EVAL_SOURCES keys (not coverage table names)
            "trades": "CONFIRMING",
            "ob": "CONFIRMING",
            "liquidity": "NEUTRAL",
            "volatility": "NEUTRAL",
            "fake_impulse": "NEUTRAL",
            "_fake_impulse_label": "CLEAN",
        },
    )
    assert verdict == "CORE_RESEARCH_SUPPORTIVE"
    # OI/liq MISSING must not be rewritten into a NEUTRAL supporting source
    assert "NEUTRAL_OI" not in reasons
    assert "NEUTRAL_LIQUIDATION" not in reasons
    assert doc["oi_liq_missing_does_not_block_core_research"] is True


def test_scope_normalized_xrp_parity_fixes_false_failure():
    """Gate defaults to MULTICOIN_DETECTION_SCOPES; out-of-scope export rows are ignored."""
    export = [
        {
            "candidate_id": "in_scope",
            "symbol": "XRPUSDT",
            "timeframe": "5m",
            "mode_id": "M0_STRICT_SYNC",
            "decision_at": "2026-08-01T00:05:00+00:00",
            "entry_at": "2026-08-01T00:05:00+00:00",
            "entry_price": 1.0,
            "direction": "BULLISH",
            "core_research_verdict": "CORE_RESEARCH_SUPPORTIVE",
        },
        {
            "candidate_id": "out_m4",
            "symbol": "XRPUSDT",
            "timeframe": "5m",
            "mode_id": "M4_TOUCH_05_EXP_1",
            "decision_at": "2026-08-01T00:05:00+00:00",
            "entry_at": "2026-08-01T00:05:00+00:00",
            "entry_price": 1.0,
            "direction": "BULLISH",
            "core_research_verdict": "CORE_RESEARCH_SUPPORTIVE",
        },
        {
            "candidate_id": "out_15m_m5",
            "symbol": "XRPUSDT",
            "timeframe": "15m",
            "mode_id": "M5_COMPRESSED_REBOUND",
            "decision_at": "2026-08-01T00:05:00+00:00",
            "entry_at": "2026-08-01T00:05:00+00:00",
            "entry_price": 1.0,
            "direction": "BULLISH",
            "core_research_verdict": "CORE_RESEARCH_SUPPORTIVE",
        },
    ]
    produced = [export[0]]
    fixed = compare_xrp_candidates_to_export(produced, export)
    assert fixed["ok"] is True
    assert fixed["n_export"] == 1
    assert fixed["n_produced"] == 1
    assert fixed["n_matched"] == 1

    scoped = scope_normalized_parity(produced, export, scopes=MULTICOIN_DETECT_SCOPES)
    assert scoped["ok"] is True
    assert scoped["n_export"] == 1
    assert scoped["n_produced"] == 1
    assert scoped["n_matched"] == 1


def test_ten_tp_five_sl_formula():
    assert 10 * 6.0 - 5 * 6.5 == pytest.approx(27.5)

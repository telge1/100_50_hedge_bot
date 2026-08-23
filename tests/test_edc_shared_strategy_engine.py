"""Tests for shared frozen EDC strategy engine (synthetic; no ClickHouse)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation.xrp_parity import (
    compare_xrp_candidates_to_export,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.shared_strategy.entry import (
    next_signal_tf_open,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.shared_strategy.outcomes import (
    simulate_canonical_trade,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.shared_strategy.semantics import (
    ENTRY_RULE,
    MULTICOIN_DETECTION_SCOPES,
    OUTCOME_PAD_HOURS,
    REQUIRE_FULL_HORIZON,
    WARMUP_PAD_DAYS,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.tpsl_pnl_engine import (
    simulate_tpsl_trade,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.multicoin_frozen_validation import (
    constants as MC,
)

UTC = timezone.utc


def _tf_bars(n: int, start: datetime, minutes: int = 5, price: float = 100.0) -> pd.DataFrame:
    rows = []
    px = price
    for i in range(n):
        o = px
        rows.append(
            {
                "open_time": start + timedelta(minutes=minutes * i),
                "open": o,
                "high": o + 1.0,
                "low": o - 1.0,
                "close": o + 0.1,
                "volume": 1.0,
            }
        )
        px = o + 0.1
    return pd.DataFrame(rows)


def _1m_path(entry: datetime, n: int, *, high=None, low=None, close=None) -> pd.DataFrame:
    rows = []
    for i in range(n):
        rows.append(
            {
                "open_time": entry + timedelta(minutes=i),
                "open": 100.0,
                "high": high if high is not None else 100.5,
                "low": low if low is not None else 99.5,
                "close": close if close is not None else 100.0,
                "volume": 1.0,
            }
        )
    return pd.DataFrame(rows)


def test_entry_next_tf_open_on_5m_boundary():
    start = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    df = _tf_bars(5, start, minutes=5)
    # signal bar index 1 closes at start+10m = decision_at
    entry_at, px = next_signal_tf_open(df, 1)
    assert entry_at == start + timedelta(minutes=10)
    assert px == pytest.approx(float(df.iloc[2]["open"]))


def test_entry_between_1m_opens_uses_tf_next_not_mid_minute():
    """Canonical entry is signal-TF next open, not arbitrary 1m mid-bar."""
    start = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    df = _tf_bars(4, start, minutes=5)
    entry_at, _ = next_signal_tf_open(df, 0)
    assert entry_at.minute % 5 == 0


def test_sl_first_same_bar():
    entry = datetime(2026, 8, 1, tzinfo=UTC)
    # both TP and SL hit in first bar
    c1m = _1m_path(entry, 10, high=101.0, low=99.0)
    sim = simulate_canonical_trade(
        c1m,
        direction="BULLISH",
        entry_at=entry,
        entry_price=100.0,
        tp_pct=0.75,
        sl_pct=0.50,
        horizon_min=480,
    )
    assert sim["exit_reason"] == "SL_EXIT"
    assert sim["same_bar_conflict"] is True


def test_incomplete_horizon_not_time():
    entry = datetime(2026, 8, 1, tzinfo=UTC)
    # path too short; keep range inside TP/SL so exit is not hit
    c1m = _1m_path(entry, 30, high=100.2, low=99.8, close=100.0)
    sim = simulate_canonical_trade(
        c1m,
        direction="BULLISH",
        entry_at=entry,
        entry_price=100.0,
        tp_pct=0.75,
        sl_pct=0.50,
        horizon_min=480,
    )
    assert sim["exit_reason"] == "INCOMPLETE_OUTCOME_HORIZON"
    assert sim["include_in_primary_pnl"] is False
    assert sim["gross_return_pct"] is None


def test_candidate_near_window_end_incomplete():
    entry = datetime(2026, 8, 22, 20, 0, tzinfo=UTC)
    c1m = _1m_path(entry, 60, high=100.2, low=99.8, close=100.0)
    sim = simulate_canonical_trade(
        c1m,
        direction="BEARISH",
        entry_at=entry,
        entry_price=100.0,
        tp_pct=0.75,
        sl_pct=0.50,
        horizon_min=480,
    )
    assert sim["exit_reason"] == "INCOMPLETE_OUTCOME_HORIZON"


def test_fees_notional_rounding():
    entry = datetime(2026, 8, 1, tzinfo=UTC)
    # force TP on first bar
    c1m = _1m_path(entry, 480, high=101.0, low=99.9)
    sim = simulate_canonical_trade(
        c1m,
        direction="BULLISH",
        entry_at=entry,
        entry_price=100.0,
        tp_pct=0.75,
        sl_pct=0.50,
        horizon_min=480,
    )
    assert sim["exit_reason"] == "TP_EXIT"
    assert sim["costs_usdt"] == pytest.approx(1.5)
    assert sim["net_pnl_usdt"] == pytest.approx(6.0)
    assert sim["notional_usdt"] == 1000.0


def test_pads_and_horizon_flag_canonical():
    assert REQUIRE_FULL_HORIZON is False
    assert MC.REQUIRE_FULL_HORIZON is False
    assert WARMUP_PAD_DAYS == MC.WARMUP_PAD_DAYS == 5
    assert OUTCOME_PAD_HOURS == MC.OUTCOME_PAD_HOURS == 12
    assert ENTRY_RULE == MC.ENTRY_RULE == "SIGNAL_TF_NEXT_OPEN_AFTER_SIGNAL_BAR"


def test_parity_gate_scope_excludes_out_of_scope():
    produced = [
        {
            "candidate_id": "a",
            "symbol": "XRPUSDT",
            "timeframe": "5m",
            "mode_id": "M0_STRICT_SYNC",
            "decision_at": "2026-08-01T00:05:00+00:00",
            "entry_at": "2026-08-01T00:05:00+00:00",
            "entry_price": 1.0,
            "direction": "BULLISH",
            "core_research_verdict": "CORE_RESEARCH_SUPPORTIVE",
        }
    ]
    export = produced + [
        {
            "candidate_id": "out_of_scope",
            "symbol": "XRPUSDT",
            "timeframe": "5m",
            "mode_id": "M4_TOUCH_05_EXP_1",  # not in multicoin 5m scope
            "decision_at": "2026-08-01T00:05:00+00:00",
            "entry_at": "2026-08-01T00:05:00+00:00",
            "entry_price": 1.0,
            "direction": "BULLISH",
            "core_research_verdict": "CORE_RESEARCH_SUPPORTIVE",
        }
    ]
    res = compare_xrp_candidates_to_export(produced, export)
    assert res["ok"] is True
    assert res["n_export"] == 1
    assert "out_of_scope" not in (res.get("missing_in_produced") or [])


def test_xrp_audit_isolated_from_50coin_and_no_gate_leak():
    """XRP audit isolation: scoped gate, no expansion to 50-coin universe, no state leak."""
    assert ("5m", "M0_STRICT_SYNC") in MULTICOIN_DETECTION_SCOPES
    assert len(MULTICOIN_DETECTION_SCOPES) == 3
    # Multi-coin symbols must never enter the XRP export comparator
    produced = [
        {
            "candidate_id": "xrp_only",
            "symbol": "XRPUSDT",
            "timeframe": "5m",
            "mode_id": "M0_STRICT_SYNC",
            "decision_at": "2026-08-01T00:05:00+00:00",
            "entry_at": "2026-08-01T00:05:00+00:00",
            "entry_price": 1.0,
            "direction": "BULLISH",
            "core_research_verdict": "CORE_RESEARCH_SUPPORTIVE",
        }
    ]
    polluted_export = produced + [
        {
            "candidate_id": "btc_leak",
            "symbol": "BTCUSDT",
            "timeframe": "5m",
            "mode_id": "M0_STRICT_SYNC",
            "decision_at": "2026-08-01T00:05:00+00:00",
            "entry_at": "2026-08-01T00:05:00+00:00",
            "entry_price": 1.0,
            "direction": "BULLISH",
            "core_research_verdict": "CORE_RESEARCH_SUPPORTIVE",
        }
    ]
    r1 = compare_xrp_candidates_to_export(produced, polluted_export)
    assert r1["ok"] is True
    assert r1["n_export"] == 1
    # Second call must not retain prior rows / mutate shared scope
    r2 = compare_xrp_candidates_to_export(produced, produced)
    assert r2["ok"] is True
    assert r2["n_export"] == 1
    assert list(MULTICOIN_DETECTION_SCOPES) == [
        ("5m", "M0_STRICT_SYNC"),
        ("5m", "M5_COMPRESSED_REBOUND"),
        ("15m", "M4_TOUCH_05_EXP_1"),
    ]
    # Custom scopes must not leak into subsequent default calls
    r3 = compare_xrp_candidates_to_export(
        produced,
        polluted_export,
        scopes=(("5m", "M0_STRICT_SYNC"),),
    )
    assert r3["ok"] is True
    r4 = compare_xrp_candidates_to_export(produced, polluted_export)
    assert r4["ok"] is True
    assert sorted(r4["scopes"]) == sorted(MULTICOIN_DETECTION_SCOPES)


def test_redetect_idempotent_entry_helper():
    start = datetime(2026, 7, 24, tzinfo=UTC)
    df = _tf_bars(10, start)
    a1, p1 = next_signal_tf_open(df, 3)
    a2, p2 = next_signal_tf_open(df, 3)
    assert a1 == a2 and p1 == p2


def test_engine_incomplete_flag_matches_canonical_wrapper():
    entry = datetime(2026, 8, 1, tzinfo=UTC)
    c1m = _1m_path(entry, 10, high=100.2, low=99.8, close=100.0)
    direct = simulate_tpsl_trade(
        c1m,
        direction="BULLISH",
        entry_at=entry,
        entry_price=100.0,
        tp_pct=0.75,
        sl_pct=0.50,
        horizon_min=480,
        require_full_horizon=False,
        incomplete_if_truncated_path=True,
    )
    wrapped = simulate_canonical_trade(
        c1m,
        direction="BULLISH",
        entry_at=entry,
        entry_price=100.0,
        tp_pct=0.75,
        sl_pct=0.50,
        horizon_min=480,
    )
    assert direct["exit_reason"] == wrapped["exit_reason"] == "INCOMPLETE_OUTCOME_HORIZON"

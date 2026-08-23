"""Phase-1 tests: M0/M1 bar-gap sync tolerance (research-only)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from orderbook_analyse.ema_dual_cross_multisource.config import EMA_DUAL_CROSS_DEFAULTS
from orderbook_analyse.ema_dual_cross_multisource.coverage_gate import assess_coverage
from orderbook_analyse.ema_dual_cross_multisource.gate_policy import apply_gate
from orderbook_analyse.ema_dual_cross_multisource.models import FinalVerdict
from orderbook_analyse.ema_dual_cross_multisource.timeframes import bar_close
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.detect_bar_gap import (
    detect_bar_gap_sync,
    detect_strict_sync_baseline,
)
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.episode_id import make_cross_episode_id
from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.tpsl import (
    simulate_tpsl_trade,
    summarize_trade_pnl,
)


def _bar(
    i: int,
    *,
    e9: float,
    e20: float,
    e59: float = 1.0,
    close: float | None = None,
    s9: float = 0.002,
    s20: float = 0.002,
    s59: float = 0.0002,
    atr_hint: float = 0.05,
    tf_min: int = 15,
) -> dict:
    c = close if close is not None else e9
    return {
        "open_time": (datetime(2026, 8, 1, tzinfo=timezone.utc) + timedelta(minutes=tf_min * i)).replace(tzinfo=None),
        "open": c,
        "high": c + atr_hint * 0.4,
        "low": c - atr_hint * 0.4,
        "close": c,
        "volume": 1000.0,
        "ema_9": e9,
        "ema_20": e20,
        "ema_59": e59,
        "ema_9_slope_1": s9,
        "ema_20_slope_1": s20,
        "ema_59_slope_1": s59,
        "atr": atr_hint,
    }


def _warm_below(n: int = 85) -> list[dict]:
    return [_bar(i, e9=0.9990, e20=0.9991, e59=1.0, close=0.9990) for i in range(n)]


def _scenario(bars: list[dict]) -> pd.DataFrame:
    # pad so detect loops process signal bars (needs i < len-1)
    bars = list(bars) + [_bar(len(bars), e9=1.002, e20=1.0015, e59=1.0, close=1.002)]
    return pd.DataFrame(bars)


def test_m1_gap0_matches_m0_keys():
    bars = _warm_below()
    t = len(bars)
    # same-bar bull sync — tight band
    bars.append(_bar(t, e9=1.0006, e20=1.0004, e59=1.0, close=1.0007, s9=0.003, s20=0.002))
    df = _scenario(bars)
    m0 = detect_strict_sync_baseline(df, symbol="XRPUSDT", timeframe="15m")
    m1 = detect_bar_gap_sync(df, symbol="XRPUSDT", timeframe="15m", max_gap=0)
    k0 = {(c["direction"], int(c["bar_index"]), int(c["exact_gap"])) for c in m0}
    k1 = {(c["direction"], int(c["bar_index"]), int(c["exact_gap"])) for c in m1}
    assert k0 == k1
    assert all(c["exact_gap"] == 0 for c in m1)


def test_gap1_confirmed_only_on_second_close():
    bars = _warm_below()
    t = len(bars)
    bars.append(_bar(t, e9=1.0005, e20=0.9997, e59=1.0, close=1.0005, s9=0.003, s20=0.001))
    bars.append(_bar(t + 1, e9=1.0008, e20=1.0004, e59=1.0, close=1.0008, s9=0.003, s20=0.003))
    df = _scenario(bars)
    g0 = detect_bar_gap_sync(df, symbol="XRPUSDT", timeframe="15m", max_gap=0)
    g1 = detect_bar_gap_sync(df, symbol="XRPUSDT", timeframe="15m", max_gap=1)
    assert not any(int(c["exact_gap"]) == 1 for c in g0)
    hit = [c for c in g1 if int(c["exact_gap"]) == 1]
    assert len(hit) == 1
    assert hit[0]["bar_index"] == t + 1
    assert hit[0]["first_cross_bar"] == t


def test_gap2_confirmed_only_on_second_close():
    bars = _warm_below()
    t = len(bars)
    bars.append(_bar(t, e9=1.0005, e20=0.9997, e59=1.0, close=1.0005, s9=0.003, s20=0.001))
    bars.append(_bar(t + 1, e9=1.0006, e20=0.9998, e59=1.0, close=1.0006, s9=0.003, s20=0.001))
    bars.append(_bar(t + 2, e9=1.0008, e20=1.0004, e59=1.0, close=1.0008, s9=0.003, s20=0.003))
    df = _scenario(bars)
    g1 = detect_bar_gap_sync(df, symbol="XRPUSDT", timeframe="15m", max_gap=1)
    g2 = detect_bar_gap_sync(df, symbol="XRPUSDT", timeframe="15m", max_gap=2)
    assert not any(int(c["exact_gap"]) == 2 for c in g1)
    hit = [c for c in g2 if int(c["exact_gap"]) == 2]
    assert len(hit) == 1
    assert hit[0]["bar_index"] == t + 2
    assert hit[0]["first_cross_bar"] == t


def test_counter_cross_invalidates_pending():
    bars = _warm_below()
    t = len(bars)
    bars.append(_bar(t, e9=1.0005, e20=0.9997, e59=1.0, close=1.0005, s9=0.003, s20=0.001))
    bars.append(_bar(t + 1, e9=0.9990, e20=0.9996, e59=1.0, close=0.9990, s9=-0.004, s20=-0.001))
    bars.append(_bar(t + 2, e9=0.9988, e20=1.0004, e59=1.0, close=1.0002, s9=-0.001, s20=0.003))
    df = _scenario(bars)
    g2 = detect_bar_gap_sync(df, symbol="XRPUSDT", timeframe="15m", max_gap=2)
    assert not any(c["direction"] == "BULLISH" and int(c.get("exact_gap") or 0) > 0 for c in g2)


def test_entry_after_decision_at():
    bars = _warm_below()
    t = len(bars)
    bars.append(_bar(t, e9=1.0006, e20=1.0004, e59=1.0, close=1.0007))
    bars.append(_bar(t + 1, e9=1.0008, e20=1.0006, e59=1.0, close=1.0008))
    df = _scenario(bars)
    m0 = detect_strict_sync_baseline(df, symbol="XRPUSDT", timeframe="15m")
    assert m0
    c = m0[0]
    decision = bar_close(c["candidate_at"], "15m")

    def _to_utc(ts) -> datetime:
        if isinstance(ts, datetime):
            return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        return pd.Timestamp(ts).to_pydatetime().replace(tzinfo=timezone.utc)

    next_open = _to_utc(df.iloc[c["bar_index"] + 1]["open_time"])
    assert next_open == decision


def test_long_short_symmetry_gap1():
    def bull_case():
        bars = _warm_below()
        t = len(bars)
        bars.append(_bar(t, e9=1.0005, e20=0.9997, e59=1.0, close=1.0005, s9=0.003, s20=0.001))
        bars.append(_bar(t + 1, e9=1.0008, e20=1.0004, e59=1.0, close=1.0008, s9=0.003, s20=0.003))
        return detect_bar_gap_sync(_scenario(bars), symbol="X", timeframe="15m", max_gap=1)

    def bear_case():
        bars = [_bar(i, e9=1.0010, e20=1.0009, e59=1.0, close=1.0010) for i in range(85)]
        t = len(bars)
        bars.append(_bar(t, e9=0.9995, e20=1.0003, e59=1.0, close=0.9995, s9=-0.003, s20=-0.001))
        bars.append(_bar(t + 1, e9=0.9992, e20=0.9996, e59=1.0, close=0.9992, s9=-0.003, s20=-0.003))
        return detect_bar_gap_sync(_scenario(bars), symbol="X", timeframe="15m", max_gap=1)

    b = [c for c in bull_case() if int(c["exact_gap"]) == 1]
    s = [c for c in bear_case() if int(c["exact_gap"]) == 1]
    assert len(b) == 1 and len(s) == 1
    assert b[0]["direction"] == "BULLISH" and s[0]["direction"] == "BEARISH"
    assert int(b[0]["exact_gap"]) == int(s[0]["exact_gap"]) == 1


def test_stable_cross_episode_id():
    a = make_cross_episode_id(symbol="XRPUSDT", timeframe="15m", direction="BULLISH", first_cross_bar=10, first_leg="EMA9")
    b = make_cross_episode_id(symbol="XRPUSDT", timeframe="15m", direction="BULLISH", first_cross_bar=10, first_leg="EMA9")
    c = make_cross_episode_id(symbol="XRPUSDT", timeframe="15m", direction="BULLISH", first_cross_bar=11, first_leg="EMA9")
    assert a == b and a != c
    assert a.startswith("edx:")


def test_combined_portfolio_no_double_trade():
    # same episode appears in gap0 and gap1 modes → portfolio keeps one
    ep = make_cross_episode_id(symbol="X", timeframe="15m", direction="BULLISH", first_cross_bar=5, first_leg="BOTH")
    rows = [
        {"cross_episode_id": ep, "mode_id": "M1_GAP_1", "exact_gap": 0, "timeframe": "15m", "final_verdict": "ALLOW"},
        {"cross_episode_id": ep, "mode_id": "M0_STRICT_SYNC", "exact_gap": 0, "timeframe": "15m", "final_verdict": "ALLOW"},
    ]

    def pref_key(c):
        mrank = 0 if c["mode_id"] == "M0_STRICT_SYNC" else 1
        return (mrank, int(c["exact_gap"]))

    chosen = sorted(rows, key=pref_key)[0]
    assert chosen["mode_id"] == "M0_STRICT_SYNC"


def test_missing_stays_inconclusive():
    cov = {
        "coverage_gate": "INCONCLUSIVE_DATA",
        "critical_missing": ["open_interest", "liquidations"],
        "candles": {"status": "VALID"},
        "public_trades_cross": {"status": "VALID"},
        "orderbook_ob200_v3": {"status": "VALID"},
        "open_interest": {"status": "MISSING", "critical_for_allow": True},
        "liquidations": {"status": "MISSING", "critical_for_allow": True},
        "ready_for_allow": False,
    }
    features = {
        "windows": {"cross_candle": {}, "pre_timeframe": {}, "baseline_60m": {}},
        "volatility": {"body_atr": 0.5},
        "liquidity_confluence": {"lld_status": "EMPTY"},
        "frozen_gate_features": {},
        "trade_flow": {},
        "oi_features": {},
        "liquidation_features": {},
    }
    verdict, reasons, _ = apply_gate(direction="BULLISH", features=features, coverage=cov)
    assert verdict == FinalVerdict.INCONCLUSIVE_DATA
    assert "CRITICAL_COVERAGE_MISSING" in reasons


def test_sl_first_same_1m_bar():
    rows = []
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    # entry at 12:00, first bar ranges through both TP and SL
    rows.append({"open_time": t0.replace(tzinfo=None), "open": 100.0, "high": 100.3, "low": 99.7, "close": 100.1, "volume": 1})
    df = pd.DataFrame(rows)
    sim = simulate_tpsl_trade(
        df,
        direction="BULLISH",
        entry_at=t0,
        entry_price=100.0,
        tp_pct=0.20,
        sl_pct=0.20,
        horizon_minutes=60,
        fee_roundtrip_pct=0.15,
    )
    assert sim["exit_reason"] == "SL_FIRST"
    assert sim["exit_price"] == pytest.approx(99.8)


def test_horizon_exit_last_close():
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(5):
        ts = (t0 + timedelta(minutes=i)).replace(tzinfo=None)
        rows.append({"open_time": ts, "open": 100, "high": 100.05, "low": 99.95, "close": 100 + i * 0.01, "volume": 1})
    df = pd.DataFrame(rows)
    sim = simulate_tpsl_trade(
        df,
        direction="BULLISH",
        entry_at=t0,
        entry_price=100.0,
        tp_pct=1.0,
        sl_pct=1.0,
        horizon_minutes=3,
        fee_roundtrip_pct=0.11,
    )
    assert sim["exit_reason"] == "HORIZON"
    # bars open_time in [entry, entry+3m): 0,1,2 → last close 100.02
    assert sim["exit_price"] == pytest.approx(100.02)


def test_fee_roundtrip_once():
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    df = pd.DataFrame(
        [
            {"open_time": t0.replace(tzinfo=None), "open": 100, "high": 100.5, "low": 99.9, "close": 100.4, "volume": 1},
        ]
    )
    sim = simulate_tpsl_trade(
        df,
        direction="BULLISH",
        entry_at=t0,
        entry_price=100.0,
        tp_pct=0.30,
        sl_pct=0.50,
        horizon_minutes=60,
        fee_roundtrip_pct=0.20,
    )
    assert sim["exit_reason"] == "TP"
    assert sim["fee_pct"] == 0.20
    assert sim["net_pnl_pct"] == pytest.approx(sim["gross_pnl_pct"] - 0.20)


def test_defaults_unchanged():
    assert EMA_DUAL_CROSS_DEFAULTS.enable_sync_cross is True
    assert EMA_DUAL_CROSS_DEFAULTS.enable_compressed_rebound is False

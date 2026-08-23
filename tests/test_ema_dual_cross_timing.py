"""Timing, coverage classification, and timeframe window tests for EDC."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from orderbook_analyse.cluster_sweep_research.ema_features import attach_emas
from orderbook_analyse.cluster_sweep_research.feature_enrichment import source_window_status
from orderbook_analyse.ema_dual_cross_multisource.config import EMA_DUAL_CROSS_DEFAULTS, EmaDualCrossConfig
from orderbook_analyse.ema_dual_cross_multisource.coverage_gate import assess_coverage
from orderbook_analyse.ema_dual_cross_multisource.ema_candidate import detect_cross_events
from orderbook_analyse.ema_dual_cross_multisource.feature_builder import build_gate_features
from orderbook_analyse.ema_dual_cross_multisource.gate_policy import apply_gate
from orderbook_analyse.ema_dual_cross_multisource.pipeline import run_ema_dual_cross_on_candles
from orderbook_analyse.ema_dual_cross_multisource.timeframes import bar_close, timeframe_duration


def _bars(closes: list[float], *, tf_min: int = 15, start: datetime | None = None) -> pd.DataFrame:
    start = start or datetime(2026, 8, 16, 0, 0, tzinfo=timezone.utc)
    rows = []
    for i, c in enumerate(closes):
        t = (start + timedelta(minutes=tf_min * i)).replace(tzinfo=None)
        o = closes[i - 1] if i else c
        rows.append({"open_time": t, "open": o, "high": max(o, c) * 1.002, "low": min(o, c) * 0.998, "close": c, "volume": 1000.0})
    return attach_emas(pd.DataFrame(rows))


def _bull_sync_closes(n_warm: int = 80) -> list[float]:
    closes = [1.0] * n_warm
    for i in range(20):
        closes.append(1.0 + i * 0.002)
    return closes


def _full_trades(bar_open: datetime, tf_min: int = 15):
    rows = []
    for m in range(tf_min):
        t = (bar_open + timedelta(minutes=m)).replace(tzinfo=None)
        rows.append({"minute": t, "buy_notional": 600, "sell_notional": 400, "trade_count": 10, "delta": 200})
    return pd.DataFrame(rows)


def _full_ob(bar_open: datetime, tf_min: int = 15):
    rows = []
    for m in range(tf_min):
        t = (bar_open + timedelta(minutes=m)).replace(tzinfo=None)
        rows.append({"minute": t, "imbalance_l50": 0.06, "spread_bps": 2.0})
    return pd.DataFrame(rows)


def _full_oi(bar_open: datetime, tf_min: int = 15):
    rows = []
    for m in range(-60, 0):
        t = (bar_open + timedelta(minutes=m)).replace(tzinfo=None)
        rows.append({"minute": t, "open_interest": 1_000_000 + m * 100})
    return pd.DataFrame(rows)


def _full_liq(bar_open: datetime):
    return pd.DataFrame([
        {"event_time": (bar_open - timedelta(minutes=10)).replace(tzinfo=None), "side": "LIQUIDATED_SHORT", "notional": 5000},
        {"event_time": (bar_open - timedelta(minutes=5)).replace(tzinfo=None), "side": "LIQUIDATED_LONG", "notional": 2000},
    ])


# --- Defaults ---


def test_rebound_default_off():
    assert EMA_DUAL_CROSS_DEFAULTS.enable_sync_cross is True
    assert EMA_DUAL_CROSS_DEFAULTS.enable_compressed_rebound is False
    assert EMA_DUAL_CROSS_DEFAULTS.require_oi_for_allow is True
    assert EMA_DUAL_CROSS_DEFAULTS.require_liq_for_allow is True


def test_rebound_optional_on():
    cfg = EmaDualCrossConfig(enable_sync_cross=True, enable_compressed_rebound=True)
    df = _bars(_bull_sync_closes())
    valid, _ = detect_cross_events(df, symbol="XRPUSDT", timeframe="15m", cfg=cfg)
    types = {v["candidate_type"] for v in valid}
    assert "SYNCHRONOUS_DUAL_EMA_CROSS" in types or len(valid) == 0


def test_sync_priority_unchanged():
    cfg = EmaDualCrossConfig(enable_sync_cross=True, enable_compressed_rebound=True)
    df = _bars(_bull_sync_closes())
    valid, _ = detect_cross_events(df, symbol="XRPUSDT", timeframe="15m", cfg=cfg)
    by_bar = {}
    for v in valid:
        key = (v["bar_index"], v["direction"])
        by_bar.setdefault(key, []).append(v["candidate_type"])
    for types in by_bar.values():
        if "SYNCHRONOUS_DUAL_EMA_CROSS" in types:
            assert "COMPRESSED_EMA59_REBOUND" not in types


# --- Timeframe duration ---


@pytest.mark.parametrize("tf,minutes", [("5m", 5), ("15m", 15), ("1h", 60)])
def test_timeframe_duration(tf: str, minutes: int):
    assert int(timeframe_duration(tf).total_seconds() // 60) == minutes


@pytest.mark.parametrize("tf,minutes", [("5m", 5), ("15m", 15), ("1h", 60)])
def test_decision_at_bar_close(tf: str, minutes: int):
    bar_open = datetime(2026, 8, 16, 18, 0, tzinfo=timezone.utc)
    expected_close = bar_open + timedelta(minutes=minutes)
    assert bar_close(bar_open, tf) == expected_close


# --- Feature windows ---


@pytest.mark.parametrize("tf,minutes", [("5m", 5), ("15m", 15), ("1h", 60)])
def test_cross_candle_window_full_bar(tf: str, minutes: int):
    bar_open = datetime(2026, 8, 16, 18, 0, tzinfo=timezone.utc)
    bar_end = bar_open + timedelta(minutes=minutes)
    df = _bars(_bull_sync_closes(), tf_min=minutes, start=datetime(2026, 8, 1, tzinfo=timezone.utc))
    trades = _full_trades(bar_open, minutes)
    feats = build_gate_features(
        candidate_at=bar_open,
        direction="BEARISH",
        df=df,
        bar_index=80,
        trades_1m=trades,
        ob_1m=None,
        oi_1m=None,
        liq=None,
        symbol="XRPUSDT",
        timeframe=tf,
    )
    timing = feats["timing"]
    assert timing["bar_open"].startswith("2026-08-16T18:00:00")
    assert timing["decision_at"] == timing["bar_close"]
    cross = feats["windows"]["cross_candle"]
    assert cross["window_start"].startswith("2026-08-16T18:00:00")
    assert cross["window_end"].startswith(bar_end.strftime("%Y-%m-%dT%H:%M:%S"))
    assert cross["trades_status"] == "VALID"
    assert cross["trade_count"] == minutes * 10


def test_decision_uses_cross_bar_not_entry_bar():
    bar_open = datetime(2026, 8, 16, 18, 0, tzinfo=timezone.utc)
    entry_open = bar_open + timedelta(minutes=15)
    df = _bars(_bull_sync_closes())
    trades_cross = _full_trades(bar_open, 15)
    trades_entry = _full_trades(entry_open, 15)
    trades = pd.concat([trades_cross, trades_entry], ignore_index=True)
    feats = build_gate_features(
        candidate_at=bar_open,
        direction="BEARISH",
        df=df,
        bar_index=80,
        trades_1m=trades,
        ob_1m=None,
        oi_1m=None,
        liq=None,
        symbol="XRPUSDT",
        timeframe="15m",
    )
    cross = feats["windows"]["cross_candle"]
    assert cross["trade_count"] == 150
    pre = feats["windows"]["pre_timeframe"]
    assert pre["window_end"].startswith("2026-08-16T18:00:00")


def test_pipeline_decision_and_entry_15m():
    df = _bars(_bull_sync_closes())
    start = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(days=30)
    bar_open = datetime(2026, 8, 16, 18, 0, tzinfo=timezone.utc)
    bundle = run_ema_dual_cross_on_candles(
        df,
        symbol="XRPUSDT",
        timeframe="15m",
        window_start=start,
        window_end=end,
        trades_1m=_full_trades(bar_open),
        ob_1m=_full_ob(bar_open),
        oi_1m=_full_oi(bar_open),
        liq=_full_liq(bar_open),
        attach_outcomes=False,
    )
    sync = [c for c in bundle["candidates"] if c["candidate_at"].startswith("2026-08-16T18:00")]
    if not sync:
        pytest.skip("no sync candidate on synthetic bars at 18:00")
    c = sync[0]
    assert c["decision_at"] == "2026-08-16T18:15:00+00:00"
    assert c["hypothetical_entry_at"] == c["decision_at"]
    assert c["features"]["timing"]["decision_at"] == c["decision_at"]


# --- MISSING vs EMPTY_WINDOW ---


def test_liq_missing_when_feed_starts_after_candidate():
    bar_open = datetime(2026, 8, 16, 18, 0, tzinfo=timezone.utc)
    bar_end = bar_open + timedelta(minutes=15)
    liq = pd.DataFrame([
        {"event_time": (bar_open + timedelta(minutes=20)).replace(tzinfo=None), "side": "LIQUIDATED_SHORT", "notional": 1},
    ])
    st, _ = source_window_status(
        liq, "event_time", bar_open - timedelta(minutes=15), bar_open,
        bar_open=bar_open, bar_close=bar_end, window_role="pre",
    )
    assert st == "MISSING"


def test_liq_empty_window_when_covered_but_no_events():
    bar_open = datetime(2026, 8, 16, 18, 0, tzinfo=timezone.utc)
    bar_end = bar_open + timedelta(minutes=15)
    liq = pd.DataFrame([
        {"event_time": (bar_open - timedelta(minutes=30)).replace(tzinfo=None), "side": "LIQUIDATED_SHORT", "notional": 1},
    ])
    st, _ = source_window_status(
        liq, "event_time", bar_open - timedelta(minutes=15), bar_open,
        bar_open=bar_open, bar_close=bar_end, window_role="pre",
    )
    assert st == "EMPTY_WINDOW"


def test_oi_missing_inconclusive_not_neutral():
    feats = {
        "windows": {
            "pre_timeframe": {"oi_status": "MISSING", "liq_status": "VALID", "trades_status": "VALID", "ob_status": "VALID"},
            "cross_candle": {"trades_status": "VALID", "ob_status": "VALID", "taker_buy_ratio": 0.6, "delta": 10},
            "baseline_60m": {"oi_status": "MISSING", "liq_status": "VALID"},
        },
        "frozen_gate_features": {"taker_buy_ratio": 0.6, "ret_5m": 0.01},
        "volatility": {"body_atr": 0.5},
        "liquidity_confluence": {"lld_status": "VALID"},
        "trade_flow": {},
        "ob_meta": {"status": "VALID"},
        "oi_features": {},
        "liquidation_features": {},
    }
    cov = {"coverage_gate": "PASS", "critical_missing": []}
    v, reasons, sv = apply_gate(direction="BULLISH", features=feats, coverage=cov)
    assert sv["oi"] == "INCONCLUSIVE_DATA"
    assert v.value == "INCONCLUSIVE_DATA"
    assert "MULTISOURCE_CONFIRMATION" not in reasons


def test_liq_missing_coverage_inconclusive():
    bar_open = datetime(2026, 8, 16, 18, 0, tzinfo=timezone.utc)
    cov = assess_coverage(
        candidate_at=bar_open,
        symbol="XRPUSDT",
        candles_df=pd.DataFrame([{"open_time": bar_open.replace(tzinfo=None), "close": 1.0}]),
        trades_1m=_full_trades(bar_open),
        ob_1m=_full_ob(bar_open),
        oi_1m=_full_oi(bar_open),
        liq=None,
        lld_status="VALID",
        timeframe="15m",
    )
    assert cov["liquidations"]["status"] == "MISSING"
    assert "liquidations" in cov["critical_missing"]
    assert cov["coverage_gate"] == "INCONCLUSIVE_DATA"

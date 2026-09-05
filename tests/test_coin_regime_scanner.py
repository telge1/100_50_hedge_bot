"""Unit tests for coin_regime_scanner (no ClickHouse)."""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from orderbook_analyse.coin_regime_scanner.classify import (
    absorption_gate,
    build_coin_regime,
    classify_breakout_readiness,
    classify_market_alignment,
    classify_momentum,
    classify_ob,
    classify_range,
    classify_trend,
    classify_vol,
    strategy_gates,
)
from orderbook_analyse.coin_regime_scanner.features import (
    close_return,
    last_row_features,
    merge_frame,
    range_60_metrics,
    realized_vol,
)
from orderbook_analyse.coin_regime_scanner.runner import universe_symbols


def _candles(
    n: int = 1600,
    *,
    base: float = 100.0,
    drift: float = 0.0,
    noise: float = 0.0,
    start: datetime | None = None,
) -> pd.DataFrame:
    t0 = start or datetime(2026, 8, 16, 0, 0, 0)
    times = [t0 + timedelta(minutes=i) for i in range(n)]
    closes = []
    px = base
    rng = np.random.default_rng(42)
    for i in range(n):
        px = px * (1.0 + drift + (noise * float(rng.normal()) if noise else 0.0))
        closes.append(px)
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "open_time": times,
            "open": closes,
            "high": closes * 1.001,
            "low": closes * 0.999,
            "close": closes,
            "volume": np.ones(n),
        }
    )


def test_universe_51_no_xau():
    syms = universe_symbols()
    assert len(syms) == 51
    assert "XAUUSDT" not in syms
    assert "BTCUSDT" in syms
    assert "XAUTUSDT" in syms
    assert "PAXGUSDT" in syms


def test_close_return_and_rv():
    c = np.array([100.0, 101.0, 102.0, 103.0, 104.0], dtype=float)
    assert close_return(c, 2) == pytest.approx(104 / 102 - 1)
    assert realized_vol(c, 3) > 0


def test_range_60_near_edge():
    highs = np.full(60, 101.0)
    lows = np.full(60, 99.0)
    closes = np.full(60, 100.95)
    m = range_60_metrics(highs, lows, closes)
    assert m["ok"]
    assert m["near_high"] is True
    assert m["width"] == pytest.approx(0.02)


def test_classify_vol_tertiles():
    feat = {
        "rv_60m_now": 0.01,
        "rv_60m": 0.01,
        "rv_15m": 0.005,
        "rv_24h": 0.008,
        "rv_60m_p33": 0.005,
        "rv_60m_p66": 0.015,
    }
    assert classify_vol(feat)[0] == "normal"
    feat["rv_60m_now"] = 0.02
    assert classify_vol(feat)[0] == "high"
    feat["rv_60m_now"] = 0.001
    assert classify_vol(feat)[0] == "low"


def test_classify_trend_bullish():
    feat = {
        "ret_15m": 0.01,
        "ret_1h": 0.02,
        "ret_4h": 0.03,
        "ema20_slope_15": 0.01,
    }
    assert classify_trend(feat)[0] == "bullish"
    feat = {
        "ret_15m": -0.01,
        "ret_1h": -0.02,
        "ret_4h": -0.03,
        "ema20_slope_15": -0.01,
    }
    assert classify_trend(feat)[0] == "bearish"


def test_classify_range_trend_share():
    feat = {
        "width_rank": 0.4,
        "range": {
            "ok": True,
            "width": 0.02,
            "touches_up": 2,
            "touches_dn": 2,
            "trend_share": 0.7,
            "ret_w": 0.014,
        },
    }
    assert classify_range(feat)[0] == "trend"
    feat["range"]["trend_share"] = 0.2
    assert classify_range(feat)[0] == "range"


def test_market_alignment():
    coin = {"ret_1h": 0.01, "ret_15m": 0.005}
    btc = {"ret_1h": 0.008, "ret_15m": 0.004}
    assert classify_market_alignment(coin, btc)[0] == "aligned"
    btc["ret_1h"] = -0.008
    assert classify_market_alignment(coin, btc)[0] == "against"


def test_ob_supportive():
    feat = {
        "ob_ok": True,
        "imbalance_l50": 0.2,
        "ofi_5m": 1.0,
        "spread_bps": 2.0,
        "ret_1h": 0.01,
    }
    assert classify_ob(feat, "bullish")[0] == "supportive"
    feat["imbalance_l50"] = -0.3
    feat["ofi_5m"] = -1.0
    assert classify_ob(feat, "bullish")[0] == "against"


def test_breakout_watch_and_active():
    feat = {
        "range": {
            "near_high": True,
            "near_low": False,
            "outside_high": False,
            "outside_low": False,
        },
        "trades_ok": True,
    }
    st, _ = classify_breakout_readiness(
        range_regime="range",
        vol_regime="normal",
        market_alignment="aligned",
        feat=feat,
        momentum="quiet",
    )
    assert st == "watch"
    feat["range"]["outside_high"] = True
    st, _ = classify_breakout_readiness(
        range_regime="range",
        vol_regime="high",
        market_alignment="neutral",
        feat=feat,
        momentum="expanding",
    )
    assert st == "active"


def test_strategy_gates_block_choppy_low_vol():
    g = strategy_gates(
        vol="low",
        trend="neutral",
        range_r="choppy",
        momentum="quiet",
        market="against",
        ob="against",
        breakout="none",
        candles_ok=True,
    )
    assert g["range60_breakout_ob"]["state"] == "block"
    assert g["trend_flag_breakout"]["state"] == "block"


def test_strategy_gates_allow_range60():
    g = strategy_gates(
        vol="high",
        trend="bullish",
        range_r="range",
        momentum="expanding",
        market="aligned",
        ob="supportive",
        breakout="active",
        candles_ok=True,
    )
    assert g["range60_breakout_ob"]["state"] == "allow"
    assert g["trend_flag_breakout"]["state"] == "allow"


def test_absorption_watch_near_edge():
    feat = {
        "range": {"near_high": True, "near_low": False},
        "delta_5m": 10.0,
        "delta_3m": 9.0,
    }
    g = absorption_gate(feat, "fading", "neutral")
    assert g["state"] == "watch"


def test_build_coin_regime_contract():
    candles = _candles(n=1600, drift=0.00005)
    trades = pd.DataFrame(
        {
            "minute": candles["open_time"],
            "trade_count": np.full(len(candles), 80),
            "total_volume": np.ones(len(candles)),
            "aggressive_buy_volume": np.ones(len(candles)) * 0.6,
            "aggressive_sell_volume": np.ones(len(candles)) * 0.4,
            "trade_delta": np.ones(len(candles)) * 0.2,
            "tps": np.full(len(candles), 80 / 60),
            "delta_ratio": np.full(len(candles), 0.2),
        }
    )
    ob = pd.DataFrame(
        {
            "minute": candles["open_time"],
            "seconds": np.full(len(candles), 60),
            "valid_seconds": np.full(len(candles), 60),
            "spread_bps": np.full(len(candles), 2.0),
            "imbalance_l50": np.full(len(candles), 0.1),
            "ofi": np.full(len(candles), 0.5),
            "ofi_5m": np.full(len(candles), 2.5),
            "bid_depth_l50": np.ones(len(candles)),
            "ask_depth_l50": np.ones(len(candles)),
        }
    )
    merged = merge_frame(candles, trades, ob)
    feat = last_row_features(merged)
    feat["ret_5m"] = close_return(merged["close"].to_numpy(dtype=float), 5)
    btc = dict(feat)
    out = build_coin_regime(
        symbol="ADAUSDT",
        as_of="2026-08-17T23:59:00Z",
        feat=feat,
        btc_feat=btc,
        candles_ok=True,
        ob_available=True,
        trades_available=True,
    )
    assert out["symbol"] == "ADAUSDT"
    assert out["vol_regime"] in ("low", "normal", "high", "unknown")
    assert out["trend_regime"] in ("bullish", "bearish", "neutral")
    assert "range60_breakout_ob" in out["strategy_gates"]
    assert out["strategy_gates"]["range60_breakout_ob"]["state"] in ("allow", "watch", "block")
    assert out["data_quality"]["candles_ok"] is True
    assert "5m" in out["timeframes"]


def test_missing_candles_blocks_gates():
    out = build_coin_regime(
        symbol="ZZZUSDT",
        as_of="2026-08-17T23:59:00Z",
        feat={},
        btc_feat=None,
        candles_ok=False,
        ob_available=False,
        trades_available=False,
        missing_reason="insufficient_candles",
    )
    assert out["strategy_gates"]["range60_breakout_ob"]["state"] == "block"
    assert out["data_quality"]["missing_reason"] == "insufficient_candles"


def test_momentum_quiet():
    feat = {
        "trades_ok": True,
        "delta_3m": 0.0,
        "delta_5m": 0.0,
        "tps": 0.05,
        "ret_15m": 0.0,
        "ret_1h": 0.0,
    }
    assert classify_momentum(feat, "neutral")[0] == "quiet"

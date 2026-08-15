"""Backtest-only CLI overrides for LONG_ADD distance and cycle coverage buffer."""

from __future__ import annotations

from research.backtests.backtest_config_loader import resolve_backtest_config
from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.historical_backtest import normalize_candles, run_historical_backtest


def test_live_baseline_long_add_and_target_profit_usdt() -> None:
    load = resolve_backtest_config(config_source="live", signal="long", symbol="APTUSDT")
    assert float(load.config.long_fill_distance_pct) == 0.5
    assert float(load.config.target_profit_usdt) == 0.015
    assert float(load.config.tp_buffer_pct) == 0.0002
    assert float(load.config.tp_profit_target_pct) == 0.25


def test_historical_backtest_applies_long_fill_and_target_profit_overrides() -> None:
    candles = normalize_candles(
        "APTUSDT",
        load_candles_for_symbol("APTUSDT", limit=40),
    )
    result = run_historical_backtest(
        "APTUSDT",
        "long",
        candles,
        max_candles=5,
        config_source="live",
        fill_model="conservative",
        tp_profit_target_pct=0.25,
        long_fill_distance_pct=1.2,
        target_profit_usdt=0.03,
    )
    assert float(result.long_fill_distance_pct) == 1.2
    assert float(result.target_profit_usdt) == 0.03
    assert float(result.tp_profit_target_pct) == 0.25

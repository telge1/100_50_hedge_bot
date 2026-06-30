"""Tests for backtest exit PnL audit shim."""

from __future__ import annotations

import logging

from research.backtests.backtest_config_loader import resolve_backtest_config
from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.hedge_bot_original_simulator import HedgeBotOriginalSimulator
from research.backtests.historical_backtest import normalize_candles, run_historical_backtest


def test_install_exit_pnl_audit_shim_adds_missing_method() -> None:
    config_load = resolve_backtest_config(config_source="test", signal="long", symbol="BTCUSDT")
    sim = HedgeBotOriginalSimulator(
        signal="long",
        symbol="BTCUSDT",
        candle_close=100.0,
        config_load=config_load,
    )
    assert hasattr(sim.strategy, "_recompute_cycle_pnl_ledger_totals")


def test_exit_pnl_audit_does_not_warn_on_cycle_fill(caplog) -> None:
    candles = normalize_candles("APTUSDT", load_candles_for_symbol("APTUSDT", limit=1000))
    window = candles[800 : 800 + 500]
    caplog.set_level(logging.WARNING, logger="fixed_cycle_hedge_bot.fixed_cycle_strategy")
    run_historical_backtest(
        "APTUSDT",
        "long",
        window,
        max_candles=499,
        config_source="live",
        fill_model="conservative",
    )
    audit_warnings = [
        record.message
        for record in caplog.records
        if record.message == "exit_pnl_audit_failed_non_blocking"
    ]
    assert audit_warnings == []

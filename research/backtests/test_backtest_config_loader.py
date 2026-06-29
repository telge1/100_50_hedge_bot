"""Phase-10 backtest config loader tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fixed_cycle_hedge_bot.fixed_cycle_strategy import FixedCycleHedgeConfig

from research.backtests.backtest_config_loader import (
    DEFAULT_LONG_CONFIG_PATH,
    load_fixed_cycle_config_for_backtest,
    resolve_backtest_config,
)
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.run_original_hedge_backtest import main as cli_main
from research.backtests.simulated_order_book import SyntheticCandle


def test_load_live_json_preserves_original_key_names(tmp_path: Path) -> None:
    config_path = tmp_path / "fixed_cycle_config.json"
    config_path.write_text(
        json.dumps(
            {
                "symbol": "ENAUSDT",
                "price_tick_size": 0.0001,
                "tp_profit_target_pct": 0.25,
                "long_fill_distance_pct": 0.5,
                "short_fill_distance_pct": 0.15,
                "base_notional_usdt": 100.0,
                "hedge_ratio_short": 0.5,
                "strategy_side": "long",
                "strategy_class": "FixedCycleHedgeStrategy",
                "custom_live_only_key": "keep-me-in-unknown",
            }
        ),
        encoding="utf-8",
    )

    result = load_fixed_cycle_config_for_backtest(
        config_path,
        signal="long",
        symbol="APTUSDT",
    )

    assert result.config_loaded is True
    assert result.config.price_tick_size == 0.0001
    assert result.config.tp_profit_target_pct == 0.25
    assert result.config.long_fill_distance_pct == 0.5
    assert result.config.short_fill_distance_pct == 0.15
    assert result.config.base_notional_usdt == 100.0
    assert result.config.hedge_ratio_short == 0.5
    assert result.config.symbol == "APTUSDT"
    assert "custom_live_only_key" in result.config_unknown_keys
    assert "price_tick_size" in result.metadata_dict()
    assert result.metadata_dict()["price_tick_size"] == 0.0001


def test_resolve_test_config_source() -> None:
    result = resolve_backtest_config(config_source="test", signal="long", symbol="BTCUSDT")
    assert result.config_source == "test"
    assert result.config_loaded is False
    assert isinstance(result.config, FixedCycleHedgeConfig)
    assert result.config.price_tick_size == 0.1


def test_missing_file_does_not_crash(tmp_path: Path) -> None:
    result = resolve_backtest_config(
        config_source="file",
        signal="long",
        symbol="BTCUSDT",
        file_config_path=tmp_path / "missing.json",
    )
    assert result.config_loaded is False
    assert result.config_load_warning


def test_live_config_path_loads_when_present() -> None:
    live_path = Path(__file__).resolve().parents[2] / DEFAULT_LONG_CONFIG_PATH
    if not live_path.exists():
        pytest.skip(f"live config not present: {live_path}")

    result = resolve_backtest_config(
        config_source="live",
        signal="long",
        symbol="APTUSDT",
    )
    assert result.config_loaded is True
    assert result.config_source == "live"
    assert result.config.price_tick_size == pytest.approx(0.0001)
    assert result.config.tp_profit_target_pct == pytest.approx(0.25)


def test_historical_backtest_with_live_config(tmp_path: Path) -> None:
    live_path = Path(__file__).resolve().parents[2] / DEFAULT_LONG_CONFIG_PATH
    if not live_path.exists():
        pytest.skip(f"live config not present: {live_path}")

    candles = [
        SyntheticCandle(
            symbol="APTUSDT",
            open=0.6518,
            high=0.6552,
            low=0.6518,
            close=0.6518,
        )
        for _ in range(5)
    ]
    result = run_historical_backtest(
        "APTUSDT",
        "long",
        candles,
        max_candles=3,
        config_source="live",
    )
    assert result.config_source == "live"
    assert result.config_loaded is True
    assert result.price_tick_size == pytest.approx(0.0001)
    assert result.tp_profit_target_pct == pytest.approx(0.25)


def test_cli_accepts_config_source_live(tmp_path: Path) -> None:
    live_path = Path(__file__).resolve().parents[2] / DEFAULT_LONG_CONFIG_PATH
    if not live_path.exists():
        pytest.skip(f"live config not present: {live_path}")

    candles = [
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "open": 0.6518,
            "high": 0.6552,
            "low": 0.6518,
            "close": 0.6518,
        }
        for _ in range(5)
    ]
    from unittest.mock import patch

    with patch(
        "research.backtests.run_original_hedge_backtest.load_candles_for_symbol",
        return_value=candles,
    ):
        exit_code = cli_main(
            [
                "--symbol",
                "APTUSDT",
                "--direction",
                "long",
                "--limit",
                "5",
                "--config-source",
                "live",
                "--output-dir",
                str(tmp_path),
                "--no-csv",
            ]
        )
    assert exit_code == 0
    payload = json.loads((tmp_path / "APTUSDT_original_hedge_5m_results.json").read_text())
    run = payload["runs"]["long"]
    assert run["config_source"] == "live"
    assert run["price_tick_size"] == pytest.approx(0.0001)

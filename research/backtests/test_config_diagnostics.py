"""Phase-9 config and initial exit-level diagnostics tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from fixed_cycle_hedge_bot.models import StrategyIntent

from research.backtests.backtest_report import SUMMARY_CSV_FIELDS, result_to_summary_row
from research.backtests.config_diagnostics import (
    build_backtest_config_diagnostics,
    compare_backtest_config_to_live_configs,
    compute_exit_price_candidates,
    enrich_exit_intent_metadata,
    find_nearest_candidate,
    scan_strategy_attributes,
)
from research.backtests.debug_report import print_config_diagnostics_report
from research.backtests.hedge_bot_original_simulator import build_test_config, build_strategy
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.intent_diagnostics import build_intent_log_entry
from research.backtests.run_original_hedge_backtest import main as cli_main
from research.backtests.simulated_order_book import SyntheticCandle


class _DummyStrategy:
    fixed_distance = 0.2

    def __init__(self) -> None:
        self.config = {"price_tick_size": 0.1}


def test_absolute_distance_candidate_matches_trigger() -> None:
    candidates = compute_exit_price_candidates(
        entry_price=0.6518,
        config={"price_tick_size": 0.1},
    )
    nearest = find_nearest_candidate(0.8518, candidates)
    assert nearest is not None
    assert nearest["value"] == pytest.approx(0.8518)
    assert "price_tick_size" in nearest["name"]


def test_percent_candidate_entry_times_tp_multiplier() -> None:
    candidates = compute_exit_price_candidates(
        entry_price=100.0,
        config={"tp_profit_target_pct": 1.0},
    )
    nearest = find_nearest_candidate(101.0, candidates)
    assert nearest is not None
    assert nearest["value"] == pytest.approx(101.0)


def test_strategy_attribute_scan_finds_fixed_distance() -> None:
    attrs = scan_strategy_attributes(_DummyStrategy())
    assert attrs.get("fixed_distance") == 0.2


def test_intent_enrichment_contains_trigger_distance_fields() -> None:
    intent = StrategyIntent(
        side="long",
        qty=1.0,
        purpose="LONG_TP_EXIT",
        trigger_price=0.8518,
        trigger_direction=1,
        reduce_only=True,
    )
    config = build_test_config(signal="long", symbol="APTUSDT")
    excerpt = enrich_exit_intent_metadata(
        intent,
        entry_price=0.6518,
        config_source="test",
        config=config,
    )
    assert excerpt["trigger_minus_entry"] == pytest.approx(0.2, rel=1e-6)
    assert excerpt["trigger_distance_pct"] == pytest.approx(30.68395, rel=1e-3)
    assert excerpt.get("nearest_config_candidate") == pytest.approx(0.8518)


def test_build_intent_log_entry_includes_exit_enrichment() -> None:
    intent = StrategyIntent(
        side="long",
        qty=1.0,
        purpose="LONG_TP_EXIT",
        trigger_price=0.8518,
        trigger_direction=1,
        reduce_only=True,
    )
    entry = build_intent_log_entry(
        intent,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        candle_index=0,
        event_source="after_fill",
        entry_price=0.6518,
        config=build_test_config(signal="long"),
        config_source="test",
    )
    excerpt = entry["metadata_excerpt"]
    assert excerpt["entry_price_at_intent"] == 0.6518
    assert excerpt["trigger_minus_entry"] == pytest.approx(0.2, rel=1e-6)


def test_build_backtest_config_diagnostics_structure() -> None:
    strategy = build_strategy("long", build_test_config(signal="long", symbol="APTUSDT"))
    diagnostics = build_backtest_config_diagnostics(
        strategy,
        strategy.config,
        symbol="APTUSDT",
        entry_price=0.6518,
        config_source="test",
        exit_trigger_price=0.8518,
        long_qty=153.421,
        short_qty=76.71,
        long_avg=0.6518,
        short_avg=0.6518,
    )
    assert diagnostics["strategy_class"] == "FixedCycleHedgeStrategy"
    assert diagnostics["nearest_candidate_to_exit_trigger"]["value"] == pytest.approx(0.8518)
    assert "price_tick_size" in diagnostics["relevant_config"]


def test_cli_json_contains_config_diagnostics(tmp_path: Path) -> None:
    candles = [
        {
            "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
        }
        for _ in range(5)
    ]
    with patch(
        "research.backtests.run_original_hedge_backtest.load_candles_for_symbol",
        return_value=candles,
    ):
        exit_code = cli_main(
            [
                "--symbol",
                "BTCUSDT",
                "--direction",
                "long",
                "--limit",
                "5",
                "--output-dir",
                str(tmp_path),
                "--no-csv",
            ]
        )
    assert exit_code == 0
    payload = json.loads((tmp_path / "BTCUSDT_original_hedge_5m_results.json").read_text())
    run = payload["runs"]["long"]
    assert "config_diagnostics" in run
    assert run["config_diagnostics"]["config_source"]


def test_summary_csv_contains_config_fields() -> None:
    candles = [
        SyntheticCandle(
            symbol="BTCUSDT",
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
        )
        for _ in range(3)
    ]
    result = run_historical_backtest("BTCUSDT", "long", candles, max_candles=2)
    row = result_to_summary_row(result)
    for field in (
        "config_source",
        "initial_exit_trigger",
        "nearest_config_candidate",
        "nearest_config_candidate_source",
    ):
        assert field in row
        assert field in SUMMARY_CSV_FIELDS


def test_compare_backtest_config_to_live_configs_non_crashing() -> None:
    comparison = compare_backtest_config_to_live_configs(build_test_config(signal="long"))
    assert "differences" in comparison
    assert "backtest_relevant_config" in comparison


def test_apt_optional_config_diagnostics() -> None:
    from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol, symbol_to_feather_name

    apt_path = DEFAULT_DATA_DIR / symbol_to_feather_name("APTUSDT")
    if not apt_path.exists():
        pytest.skip(f"external APT feather file not present: {apt_path}")

    candles = load_candles_for_symbol("APTUSDT", limit=1000)
    result = run_historical_backtest("APTUSDT", "long", candles, max_candles=999)
    assert result.config_diagnostics
    assert result.initial_exit_trigger == pytest.approx(0.8518, rel=1e-4)
    assert result.nearest_config_candidate == pytest.approx(0.8518, rel=1e-4)


def test_print_config_diagnostics_report(capsys) -> None:
    from research.backtests.backtest_report import BacktestResult

    result = BacktestResult(
        symbol="APTUSDT",
        direction="long",
        config_diagnostics={
            "strategy_class": "FixedCycleHedgeStrategy",
            "config_source": "test",
            "entry_price": 0.6518,
            "exit_trigger_price": 0.8518,
            "relevant_config": {"price_tick_size": 0.1},
        },
        live_config_comparison={"notes": [], "differences": {}},
        exit_level_diagnostics=[],
    )
    print_config_diagnostics_report(result)
    output = capsys.readouterr().out
    assert "config_diagnostics:" in output
    assert "price_tick_size=0.1" in output

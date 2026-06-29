"""Phase-8 StrategyIntent and exit diagnostics tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from fixed_cycle_hedge_bot.models import RuntimeState, StrategyIntent

from research.backtests.backtest_report import SUMMARY_CSV_FIELDS, result_to_summary_row
from research.backtests.debug_report import print_debug_report
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.intent_diagnostics import (
    build_intent_log_entry,
    build_intent_to_order_mapping,
    diagnose_exit_level,
)
from research.backtests.hedge_bot_original_simulator import HedgeBotOriginalSimulator
from research.backtests.run_original_hedge_backtest import main as cli_main
from research.backtests.simulated_order_book import SimulatedOrderBook, SyntheticCandle


def _candle(
    *,
    index: int,
    high: float,
    low: float,
    close: float | None = None,
) -> SyntheticCandle:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return SyntheticCandle(
        symbol="BTCUSDT",
        timestamp=base.replace(hour=index),
        open=close or (high + low) / 2.0,
        high=high,
        low=low,
        close=close or (high + low) / 2.0,
    )


def test_intent_log_preserves_purpose_and_trigger() -> None:
    sim = HedgeBotOriginalSimulator(signal="long", symbol="BTCUSDT", candle_close=100.0)
    try:
        intent = StrategyIntent(
            side="long",
            qty=1.0,
            purpose="LONG_TP_EXIT",
            order_type="Market",
            reduce_only=True,
            trigger_price=123.45,
        )
        sim._log_intent(intent, event_source="after_fill", source_fill_purpose="SHORT_SL_EXIT")
        order = sim._submit_intent_with_logging(intent, replace=False, intent_log_index=0)
        assert order is not None

        intent_entry = sim.intent_log[0]
        assert intent_entry["purpose"] == "LONG_TP_EXIT"
        assert intent_entry["trigger_price"] == 123.45
        assert intent_entry["source_fill_purpose"] == "SHORT_SL_EXIT"

        submitted = next(entry for entry in sim.order_log if entry["event_type"] == "submitted")
        assert submitted["intent_purpose"] == "LONG_TP_EXIT"
        assert submitted["mapped_trigger_price"] == 123.45
        assert submitted["intent_trigger_price"] == 123.45
    finally:
        sim.close()


def test_mapping_warning_for_incomplete_intent() -> None:
    book = SimulatedOrderBook(symbol="BTCUSDT")
    intent = StrategyIntent(
        side="",
        qty=1.0,
        purpose="",
        order_type="Stop",
        trigger_price=100.0,
    )
    order, _ = book.submit_intent(intent, replace=False)
    mapping = build_intent_to_order_mapping(intent, order, intent_log_index=0)
    assert "missing_purpose" in mapping.get("mapping_warning", "")
    assert "missing_side" in mapping.get("mapping_warning", "")
    assert "missing_trigger_direction" in mapping.get("mapping_warning", "")


def test_exit_diagnostics_not_touchable_sell_trigger() -> None:
    book = SimulatedOrderBook(symbol="BTCUSDT")
    intent = StrategyIntent(
        side="long",
        qty=1.0,
        purpose="LONG_TP_EXIT",
        order_type="Market",
        reduce_only=True,
        trigger_price=100.0,
        trigger_direction=1,
    )
    order, _ = book.submit_intent(intent, replace=False)
    candles_after = [_candle(index=1, high=90.0, low=88.0)]
    diagnostic = diagnose_exit_level(order, candles_after=candles_after, created_candle_index=0)
    assert diagnostic["was_touchable_after_created"] is False
    assert diagnostic["max_high_after_created"] == 90.0
    assert diagnostic["distance_to_max_high_pct"] == pytest.approx(-10.0)


def test_exit_diagnostics_touchable_sell_trigger() -> None:
    book = SimulatedOrderBook(symbol="BTCUSDT")
    intent = StrategyIntent(
        side="long",
        qty=1.0,
        purpose="LONG_TP_EXIT",
        order_type="Market",
        reduce_only=True,
        trigger_price=100.0,
        trigger_direction=1,
    )
    order, _ = book.submit_intent(intent, replace=False)
    candles_after = [_candle(index=1, high=101.0, low=99.0)]
    diagnostic = diagnose_exit_level(order, candles_after=candles_after, created_candle_index=0)
    assert diagnostic["was_touchable_after_created"] is True
    assert diagnostic["first_touch_time_after_created"] is not None


def test_historical_backtest_includes_intent_log_and_diagnostics() -> None:
    candles = [
        SyntheticCandle(
            symbol="BTCUSDT",
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
        )
        for _ in range(5)
    ]
    result = run_historical_backtest("BTCUSDT", "long", candles, max_candles=3)
    payload = result.to_dict()
    assert "intent_log" in payload
    assert isinstance(payload["intent_log"], list)
    assert len(payload["intent_log"]) >= 1
    assert "final_active_order_diagnostics" in payload


def test_summary_csv_contains_intent_and_diagnostics_fields() -> None:
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
        "last_intent_purpose",
        "last_intent_trigger_price",
        "final_active_order_diagnostics_summary",
    ):
        assert field in row
        assert field in SUMMARY_CSV_FIELDS


def test_debug_report_prints_intent_and_exit_diagnostics(capsys) -> None:
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
    print_debug_report(result, print_intent_log=True, print_exit_diagnostics=True)
    output = capsys.readouterr().out
    assert "last_" in output and "_intents:" in output
    assert "final_active_order_diagnostics" in output
    assert "intent_log:" in output


def test_cli_writes_intent_log_fields(tmp_path: Path) -> None:
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
    assert "intent_log" in run
    assert "final_active_order_diagnostics" in run


def test_apt_optional_intent_diagnostics() -> None:
    from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol, symbol_to_feather_name

    apt_path = DEFAULT_DATA_DIR / symbol_to_feather_name("APTUSDT")
    if not apt_path.exists():
        pytest.skip(f"external APT feather file not present: {apt_path}")

    candles = load_candles_for_symbol("APTUSDT", limit=1000)
    result = run_historical_backtest("APTUSDT", "long", candles, max_candles=999)
    assert result.intent_log
    assert isinstance(result.final_active_order_diagnostics, list)
    if result.final_active_order_diagnostics:
        assert "final_order_purpose" in result.final_active_order_diagnostics[0]


def test_build_intent_log_entry_metadata_excerpt() -> None:
    intent = StrategyIntent(
        side="long",
        qty=1.0,
        purpose="LONG_TP_EXIT",
        trigger_price=1.23,
        metadata={"target_profit": 5.0, "distance_pct": 30.0, "noise": "ignored"},
    )
    entry = build_intent_log_entry(
        intent,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        candle_index=1,
        event_source="after_fill",
    )
    assert entry["metadata_excerpt"]["target_profit"] == 5.0
    assert "noise" not in entry["metadata_excerpt"]

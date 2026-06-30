"""Tests for continuous re-entry backtest mode."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from research.backtests.backtest_report import BacktestResult
from research.backtests.continuous_reentry_backtest import (
    CONTINUOUS_AGGREGATE_CSV_FIELDS,
    CONTINUOUS_SUMMARY_CSV_FIELDS,
    aggregate_continuous_results,
    continuous_trade_block_id,
    run_continuous_reentry_backtests,
    run_continuous_reentry_for_direction,
    stamp_trade_block_id,
    write_continuous_aggregate_csv,
    write_continuous_summary_csv,
)
from research.backtests.run_original_hedge_backtest import main as cli_main
from research.backtests.simulated_order_book import SyntheticCandle


def _flat_candles(n: int, *, symbol: str = "BTCUSDT") -> list[SyntheticCandle]:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        SyntheticCandle(
            symbol=symbol,
            timestamp=base,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
        )
        for _ in range(n)
    ]


def test_continuous_trade_block_id_format() -> None:
    assert continuous_trade_block_id("long", 1) == "backtest_long_continuous_trade_0001"
    assert continuous_trade_block_id("short", 12) == "backtest_short_continuous_trade_0012"


def test_stamp_trade_block_id_propagates_to_logs() -> None:
    result = BacktestResult(
        symbol="APTUSDT",
        direction="long",
        fill_log=[{"purpose": "INITIAL_LONG_ENTRY"}],
        order_log=[{"purpose": "LONG_TP_EXIT"}],
        intent_log=[{"purpose": "CYCLE_1_LONG_ADD"}],
        final_active_orders=[{"purpose": "LONG_TP_EXIT", "order_id": "o1"}],
    )
    stamp_trade_block_id(result, "backtest_long_continuous_trade_0001")
    assert result.trade_block_id == "backtest_long_continuous_trade_0001"
    assert result.fill_log[0]["trade_block_id"] == "backtest_long_continuous_trade_0001"
    assert result.final_active_orders[0]["trade_block_id"] == "backtest_long_continuous_trade_0001"
    assert result.final_strategy_state_excerpt["active_trade_block_id"] == (
        "backtest_long_continuous_trade_0001"
    )


def test_aggregate_continuous_results() -> None:
    runs = [
        BacktestResult(
            symbol="APTUSDT",
            direction="long",
            fill_model="conservative",
            config_source="live",
            final_status="closed_ok",
            exit_quality="closed_ok",
            exit_reason="flat_no_active_orders",
            realized_pnl=1.0,
            candles_processed=100,
            start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end_time=datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc),
        ),
        BacktestResult(
            symbol="APTUSDT",
            direction="long",
            fill_model="conservative",
            config_source="live",
            final_status="open",
            exit_quality="open",
            exit_reason="series_end_with_open_positions",
            realized_pnl=-0.5,
            candles_processed=50,
            start_time=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
            end_time=datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc),
        ),
    ]
    aggregates = aggregate_continuous_results(runs)
    assert len(aggregates) == 1
    row = aggregates[0]
    assert row["trades_started"] == 2
    assert row["closed_count"] == 1
    assert row["successful_closed_count"] == 1
    assert row["undercovered_final_exit_count"] == 0
    assert row["unfinished_count"] == 1
    assert row["open_count"] == 1
    assert row["closed_rate_pct"] == pytest.approx(50.0)
    assert row["total_pnl"] == pytest.approx(0.5)
    assert row["total_candles_processed"] == 150


def test_run_continuous_reentry_stops_on_unfinished(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def fake_run(symbol, direction, candles, **kwargs):
        start = calls.__len__()
        calls.append(start)
        status = "closed" if start == 0 else "open"
        return BacktestResult(
            symbol=symbol,
            direction=direction,
            final_status=status,
            exit_reason="flat_no_active_orders" if status == "closed" else "series_end_with_open_positions",
            candles_processed=10,
            fills_count=3,
        )

    monkeypatch.setattr(
        "research.backtests.continuous_reentry_backtest.run_historical_backtest",
        fake_run,
    )
    results = run_continuous_reentry_for_direction(
        "APTUSDT",
        "long",
        _flat_candles(100),
        continuous_start_index=0,
    )
    assert len(results) == 2
    assert results[0].final_status == "closed"
    assert results[1].final_status == "open"
    assert results[0].trade_number == 1
    assert results[1].trade_number == 2
    assert results[0].trade_block_id == "backtest_long_continuous_trade_0001"
    assert results[0].start_index == 0
    assert results[0].end_index == 10
    assert results[1].start_index == 11


def test_run_continuous_reentry_respects_max_trades(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(symbol, direction, candles, **kwargs):
        return BacktestResult(
            symbol=symbol,
            direction=direction,
            final_status="closed",
            exit_reason="flat_no_active_orders",
            candles_processed=5,
        )

    monkeypatch.setattr(
        "research.backtests.continuous_reentry_backtest.run_historical_backtest",
        fake_run,
    )
    results = run_continuous_reentry_for_direction(
        "APTUSDT",
        "long",
        _flat_candles(100),
        continuous_max_trades=2,
    )
    assert len(results) == 2


def test_continuous_csv_writers(tmp_path: Path) -> None:
    result = BacktestResult(
        symbol="APTUSDT",
        direction="long",
        trade_number=1,
        trade_block_id="backtest_long_continuous_trade_0001",
        start_index=0,
        end_index=120,
        final_status="closed_ok",
        exit_quality="closed_ok",
        exit_reason="flat_no_active_orders",
        realized_pnl=0.42,
        fills_count=8,
        candles_processed=120,
        final_active_order_purposes=["LONG_TP_EXIT"],
    )
    summary_path = write_continuous_summary_csv(tmp_path / "summary.csv", [result])
    aggregate_path = write_continuous_aggregate_csv(
        tmp_path / "aggregate.csv",
        aggregate_continuous_results([result]),
    )

    with summary_path.open("r", encoding="utf-8", newline="") as handle:
        summary_rows = list(csv.DictReader(handle))
    assert list(summary_rows[0].keys()) == list(CONTINUOUS_SUMMARY_CSV_FIELDS)
    assert summary_rows[0]["trade_number"] == "1"
    assert summary_rows[0]["trade_block_id"] == "backtest_long_continuous_trade_0001"

    with aggregate_path.open("r", encoding="utf-8", newline="") as handle:
        aggregate_rows = list(csv.DictReader(handle))
    assert list(aggregate_rows[0].keys()) == list(CONTINUOUS_AGGREGATE_CSV_FIELDS)
    assert aggregate_rows[0]["closed_count"] == "1"


def test_cli_continuous_reentry(tmp_path: Path) -> None:
    candles = [
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
        }
        for _ in range(30)
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
                "30",
                "--continuous-reentry",
                "--config-source",
                "test",
                "--output-dir",
                str(tmp_path),
                "--no-json",
            ]
        )
    assert exit_code == 0
    assert list(tmp_path.glob("BTCUSDT_original_hedge_5m_continuous_summary.csv"))
    assert list(tmp_path.glob("BTCUSDT_original_hedge_5m_continuous_aggregate.csv"))


def test_cli_rejects_multi_start_with_continuous() -> None:
    exit_code = cli_main(
        [
            "--symbol",
            "BTCUSDT",
            "--continuous-reentry",
            "--multi-start",
        ]
    )
    assert exit_code == 1

"""Phase-11/12 multi-start backtest tests."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from research.backtests.backtest_report import BacktestResult
from research.backtests.candle_loader import DEFAULT_DATA_DIR, symbol_to_feather_name
from research.backtests.multi_start_backtest import (
    MULTI_START_UNFINISHED_CSV_FIELDS,
    aggregate_multi_start_results,
    filter_unfinished_results,
    generate_start_indices,
    multi_start_output_paths,
    run_multi_start_backtest,
    run_multi_start_backtests,
    write_multi_start_aggregate_csv,
    write_multi_start_summary_csv,
    write_multi_start_unfinished_csv,
)
from research.backtests.run_original_hedge_backtest import main as cli_main
from research.backtests.simulated_order_book import SyntheticCandle


def test_generate_start_indices() -> None:
    indices = generate_start_indices(
        100,
        start_step_candles=10,
        window_candles=20,
        max_starts=5,
    )
    assert indices == [0, 10, 20, 30, 40]


def test_generate_start_indices_respects_max_starts() -> None:
    indices = generate_start_indices(
        1000,
        start_step_candles=50,
        window_candles=200,
        max_starts=3,
    )
    assert indices == [0, 50, 100]


def _flat_candles(n: int, *, close: float = 100.0) -> list[SyntheticCandle]:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        SyntheticCandle(
            symbol="BTCUSDT",
            timestamp=base,
            open=close,
            high=close + 0.5,
            low=close - 0.5,
            close=close,
        )
        for _ in range(n)
    ]


def test_multi_start_runner_sets_start_index() -> None:
    candles = _flat_candles(30)
    results = run_multi_start_backtest(
        "BTCUSDT",
        "long",
        candles,
        config_source="test",
        start_step_candles=5,
        window_candles=10,
        max_starts=3,
    )
    assert len(results) == 3
    assert [result.start_index for result in results] == [0, 5, 10]
    assert all(result.start_time is not None for result in results)
    assert all(result.direction == "long" for result in results)


def test_aggregate_status_categories() -> None:
    results = [
        BacktestResult(
            symbol="APTUSDT",
            direction="long",
            config_source="live",
            fill_model="conservative",
            final_status="closed",
            realized_pnl=1.0,
            fills_count=4,
            candles_processed=100,
        ),
        BacktestResult(
            symbol="APTUSDT",
            direction="long",
            config_source="live",
            fill_model="conservative",
            final_status="open",
            open_reason_detail="series_end_with_open_positions",
            exit_reason="series_end_with_open_positions",
            realized_pnl=-0.5,
            fills_count=2,
            candles_processed=50,
            final_active_order_purposes=["LONG_SL_EXIT", "SHORT_TP_EXIT"],
        ),
        BacktestResult(
            symbol="APTUSDT",
            direction="long",
            config_source="live",
            fill_model="conservative",
            final_status="max_candles",
            open_reason_detail="max_candles_reached",
            exit_reason="max_candles_reached",
            realized_pnl=0.2,
            fills_count=5,
            candles_processed=999,
            final_active_order_purposes=["CYCLE_1_SHORT_REDUCE"],
        ),
        BacktestResult(
            symbol="APTUSDT",
            direction="long",
            config_source="live",
            fill_model="conservative",
            final_status="max_candles",
            open_reason_detail="max_candles_reached",
            exit_reason="max_candles_reached",
            realized_pnl=0.1,
            fills_count=5,
            candles_processed=999,
            final_active_order_purposes=["CYCLE_1_SHORT_REDUCE"],
        ),
        BacktestResult(
            symbol="APTUSDT",
            direction="long",
            config_source="live",
            fill_model="conservative",
            final_status="error",
            open_reason_detail="exception",
            realized_pnl=0.0,
            fills_count=0,
            candles_processed=0,
        ),
    ]
    row = aggregate_multi_start_results(results)[0]
    assert row["runs"] == 5
    assert row["closed_count"] == 1
    assert row["successful_closed_count"] == 1
    assert row["open_count"] == 1
    assert row["max_candles_count"] == 2
    assert row["error_count"] == 1
    assert row["unfinished_count"] == 4
    assert row["closed_rate_pct"] == pytest.approx(20.0)
    assert row["successful_closed_rate_pct"] == pytest.approx(20.0)
    assert row["unfinished_rate_pct"] == pytest.approx(80.0)
    assert row["max_candles_rate_pct"] == pytest.approx(40.0)
    assert row["most_common_max_candles_reason"] == "max_candles_reached"
    assert row["most_common_unfinished_reason"] == "max_candles_reached"
    assert row["most_common_unfinished_active_order_purposes"] == "CYCLE_1_SHORT_REDUCE"


def test_aggregate_metrics() -> None:
    results = [
        BacktestResult(
            symbol="APTUSDT",
            direction="long",
            config_source="live",
            fill_model="conservative",
            final_status="closed",
            open_reason_detail="",
            realized_pnl=1.0,
            fills_count=4,
            candles_processed=100,
            final_active_order_purposes=[],
        ),
        BacktestResult(
            symbol="APTUSDT",
            direction="long",
            config_source="live",
            fill_model="conservative",
            final_status="open",
            open_reason_detail="series_end_with_open_positions",
            exit_reason="series_end_with_open_positions",
            realized_pnl=-0.5,
            fills_count=2,
            candles_processed=999,
            final_active_order_purposes=["LONG_SL_EXIT", "SHORT_TP_EXIT"],
        ),
        BacktestResult(
            symbol="APTUSDT",
            direction="long",
            config_source="live",
            fill_model="conservative",
            final_status="error",
            open_reason_detail="exception",
            realized_pnl=0.0,
            fills_count=0,
            candles_processed=0,
        ),
    ]
    aggregates = aggregate_multi_start_results(results)
    assert len(aggregates) == 1
    row = aggregates[0]
    assert row["runs"] == 3
    assert row["closed_count"] == 1
    assert row["open_count"] == 1
    assert row["error_count"] == 1
    assert row["unfinished_count"] == 2
    assert row["closed_rate_pct"] == pytest.approx(100.0 / 3.0)
    assert row["unfinished_rate_pct"] == pytest.approx(200.0 / 3.0)
    assert row["open_rate_pct"] == pytest.approx(100.0 / 3.0)
    assert row["total_pnl"] == pytest.approx(0.5)
    assert row["avg_pnl"] == pytest.approx(0.5 / 3.0)
    assert row["median_pnl"] == pytest.approx(0.0)
    assert row["best_pnl"] == pytest.approx(1.0)
    assert row["worst_pnl"] == pytest.approx(-0.5)
    assert row["avg_fills_count"] == pytest.approx(2.0)
    assert row["avg_candles_processed"] == pytest.approx((100 + 999 + 0) / 3.0)
    assert row["avg_duration_candles"] == pytest.approx(100.0)
    assert row["most_common_open_reason"] == "series_end_with_open_positions"
    assert row["most_common_final_active_order_purposes"] == "LONG_SL_EXIT|SHORT_TP_EXIT"
    assert row["most_common_unfinished_active_order_purposes"] == "LONG_SL_EXIT|SHORT_TP_EXIT"


def test_most_common_unfinished_active_order_purposes() -> None:
    results = [
        BacktestResult(
            symbol="APTUSDT",
            direction="short",
            config_source="live",
            fill_model="conservative",
            final_status="max_candles",
            open_reason_detail="max_candles_reached",
            final_active_order_purposes=["CYCLE_1_SHORT_REDUCE"],
        ),
        BacktestResult(
            symbol="APTUSDT",
            direction="short",
            config_source="live",
            fill_model="conservative",
            final_status="max_candles",
            open_reason_detail="max_candles_reached",
            final_active_order_purposes=["CYCLE_1_SHORT_REDUCE"],
        ),
        BacktestResult(
            symbol="APTUSDT",
            direction="short",
            config_source="live",
            fill_model="conservative",
            final_status="open",
            open_reason_detail="series_end_with_open_positions",
            final_active_order_purposes=["SHORT_TP_EXIT"],
        ),
    ]
    row = aggregate_multi_start_results(results)[0]
    assert row["most_common_unfinished_active_order_purposes"] == "CYCLE_1_SHORT_REDUCE"


def test_unfinished_csv_only_non_closed(tmp_path: Path) -> None:
    results = [
        BacktestResult(
            symbol="APTUSDT",
            direction="long",
            start_index=0,
            config_source="live",
            fill_model="conservative",
            final_status="closed",
            start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        BacktestResult(
            symbol="APTUSDT",
            direction="long",
            start_index=100,
            config_source="live",
            fill_model="conservative",
            final_status="max_candles",
            open_reason_detail="max_candles_reached",
            exit_reason="max_candles_reached",
            start_time=datetime(2026, 1, 2, tzinfo=timezone.utc),
            final_active_order_purposes=["CYCLE_1_SHORT_REDUCE"],
            price_tick_size=0.0001,
            tp_profit_target_pct=0.25,
        ),
    ]
    path = tmp_path / "unfinished.csv"
    write_multi_start_unfinished_csv(path, results)
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert list(rows[0].keys()) == list(MULTI_START_UNFINISHED_CSV_FIELDS)
    assert len(rows) == 1
    assert rows[0]["start_index"] == "100"
    assert rows[0]["final_status"] == "max_candles"
    assert rows[0]["price_tick_size"] == "0.0001"


def test_unfinished_csv_header_only_when_all_closed(tmp_path: Path) -> None:
    results = [
        BacktestResult(
            symbol="APTUSDT",
            direction="long",
            config_source="live",
            fill_model="conservative",
            final_status="closed",
        ),
    ]
    path = tmp_path / "unfinished.csv"
    write_multi_start_unfinished_csv(path, results)
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("symbol,direction")


def test_filter_unfinished_results() -> None:
    results = [
        BacktestResult(symbol="X", direction="long", final_status="closed"),
        BacktestResult(symbol="X", direction="long", final_status="open"),
    ]
    assert len(filter_unfinished_results(results)) == 1


def test_cli_multi_start_writes_outputs(tmp_path: Path) -> None:
    candles = [
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
        }
        for _ in range(25)
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
                "25",
                "--multi-start",
                "--start-step-candles",
                "5",
                "--window-candles",
                "10",
                "--max-starts",
                "3",
                "--config-source",
                "test",
                "--output-dir",
                str(tmp_path),
            ]
        )
    assert exit_code == 0

    summary_path, json_path, aggregate_path, unfinished_path = multi_start_output_paths(
        tmp_path,
        "BTCUSDT",
    )
    assert summary_path.exists()
    assert json_path.exists()
    assert aggregate_path.exists()
    assert unfinished_path.exists()

    with summary_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 3
    assert rows[0]["config_source"] == "test"
    assert rows[0]["fill_model"] == "conservative"
    assert rows[0]["start_index"] == "0"
    assert rows[1]["start_index"] == "5"

    with aggregate_path.open("r", encoding="utf-8", newline="") as handle:
        agg = next(csv.DictReader(handle))
    assert "max_candles_count" in agg
    assert "unfinished_count" in agg

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["metadata"]["start_step_candles"] == 5
    assert payload["metadata"]["max_starts"] == 3
    assert payload["metadata"]["output_files"]["unfinished_csv"] == str(unfinished_path)
    assert len(payload["runs"]) == 3
    assert "fill_log" not in payload["runs"][0]
    assert "max_candles_count" in payload["aggregate"][0]


def test_multi_start_summary_and_aggregate_csv_writers(tmp_path: Path) -> None:
    result = BacktestResult(
        symbol="APTUSDT",
        direction="long",
        start_index=0,
        window_candles=100,
        config_source="live",
        fill_model="conservative",
        start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        final_status="open",
        realized_pnl=0.1,
        fills_count=2,
        price_tick_size=0.0001,
    )
    summary_path = tmp_path / "summary.csv"
    write_multi_start_summary_csv(summary_path, [result])
    with summary_path.open("r", encoding="utf-8", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["start_index"] == "0"
    assert row["price_tick_size"] == "0.0001"

    aggregate_path = tmp_path / "aggregate.csv"
    aggregates = aggregate_multi_start_results([result])
    write_multi_start_aggregate_csv(aggregate_path, aggregates)
    assert aggregate_path.exists()


@pytest.mark.skipif(
    not (DEFAULT_DATA_DIR / symbol_to_feather_name("APTUSDT")).exists(),
    reason="APT feather file not available",
)
def test_apt_multi_start_live_smoke(tmp_path: Path) -> None:
    exit_code = cli_main(
        [
            "--symbol",
            "APTUSDT",
            "--direction",
            "long",
            "--limit",
            "1500",
            "--multi-start",
            "--max-starts",
            "3",
            "--window-candles",
            "200",
            "--start-step-candles",
            "100",
            "--config-source",
            "live",
            "--output-dir",
            str(tmp_path),
        ]
    )
    assert exit_code == 0
    _, json_path, _, unfinished_path = multi_start_output_paths(tmp_path, "APTUSDT")
    assert json_path.exists()
    assert unfinished_path.exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(payload["runs"]) == 3

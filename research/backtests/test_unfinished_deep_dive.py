"""Phase-13 unfinished deep-dive tests."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from research.backtests.backtest_report import BacktestResult
from research.backtests.candle_loader import DEFAULT_DATA_DIR, symbol_to_feather_name
from research.backtests.unfinished_deep_dive import (
    DEEP_DIVE_CSV_FIELDS,
    build_deep_dive_comparison_row,
    deep_dive_output_paths,
    parse_deep_dive_start_indices,
    run_unfinished_deep_dive_after_multi_start,
    select_unfinished_runs_for_deep_dive,
    write_unfinished_deep_dive_csv,
)
from research.backtests.run_original_hedge_backtest import main as cli_main
from research.backtests.simulated_order_book import SyntheticCandle


def test_build_deep_dive_resolved_comparison() -> None:
    original = BacktestResult(
        symbol="APTUSDT",
        direction="long",
        start_index=800,
        final_status="max_candles",
        exit_reason="max_candles_reached",
        open_reason_detail="max_candles_reached",
        realized_pnl=0.08,
        fills_count=5,
        candles_processed=999,
        final_active_order_purposes=["CYCLE_1_SHORT_REDUCE"],
        final_active_order_diagnostics=[
            {"max_high_after_created": 0.66, "min_low_after_created": 0.62},
        ],
    )
    extended = BacktestResult(
        symbol="APTUSDT",
        direction="long",
        start_index=800,
        final_status="closed",
        exit_reason="flat_no_active_orders",
        open_reason_detail="closed",
        realized_pnl=0.31,
        fills_count=7,
        candles_processed=1431,
        final_active_order_purposes=[],
        final_active_order_diagnostics=[],
    )
    row = build_deep_dive_comparison_row(
        original=original,
        extended=extended,
        original_window_candles=1000,
        extended_window_candles=3000,
    )
    assert row["resolved_with_extended_window"] is True
    assert row["additional_candles_needed"] == 432
    assert row["additional_pnl"] == pytest.approx(0.23)
    assert row["additional_fills"] == 2
    assert row["still_unfinished_reason"] == ""
    assert row["original_max_high_after_active_orders"] == pytest.approx(0.66)
    assert row["original_min_low_after_active_orders"] == pytest.approx(0.62)


def test_build_deep_dive_still_unfinished() -> None:
    original = BacktestResult(
        symbol="APTUSDT",
        direction="long",
        start_index=1300,
        final_status="max_candles",
        open_reason_detail="max_candles_reached",
        exit_reason="max_candles_reached",
        realized_pnl=0.08,
        fills_count=5,
        candles_processed=999,
        final_active_order_purposes=["CYCLE_1_SHORT_REDUCE"],
    )
    extended = BacktestResult(
        symbol="APTUSDT",
        direction="long",
        start_index=1300,
        final_status="max_candles",
        open_reason_detail="max_candles_reached",
        exit_reason="max_candles_reached",
        realized_pnl=0.09,
        fills_count=5,
        candles_processed=2999,
        final_active_order_purposes=["CYCLE_1_SHORT_REDUCE"],
    )
    row = build_deep_dive_comparison_row(
        original=original,
        extended=extended,
        original_window_candles=1000,
        extended_window_candles=3000,
    )
    assert row["resolved_with_extended_window"] is False
    assert row["additional_candles_needed"] == ""
    assert row["still_unfinished_reason"] == "max_candles_reached"
    assert row["still_unfinished_active_order_purposes"] == "CYCLE_1_SHORT_REDUCE"


def test_deep_dive_csv_writer(tmp_path: Path) -> None:
    row = build_deep_dive_comparison_row(
        original=BacktestResult(
            symbol="APTUSDT",
            direction="long",
            start_index=800,
            start_time=datetime(2026, 6, 12, tzinfo=timezone.utc),
            final_status="max_candles",
            exit_reason="max_candles_reached",
            open_reason_detail="max_candles_reached",
            realized_pnl=0.08,
            fills_count=5,
            candles_processed=999,
            final_active_order_purposes=["CYCLE_1_SHORT_REDUCE"],
        ),
        extended=BacktestResult(
            symbol="APTUSDT",
            direction="long",
            start_index=800,
            final_status="closed",
            exit_reason="flat_no_active_orders",
            open_reason_detail="closed",
            realized_pnl=0.31,
            fills_count=7,
            candles_processed=1431,
        ),
        original_window_candles=1000,
        extended_window_candles=3000,
    )
    path = tmp_path / "deep_dive.csv"
    write_unfinished_deep_dive_csv(path, [row])
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert list(rows[0].keys()) == list(DEEP_DIVE_CSV_FIELDS)
    assert rows[0]["original_final_status"] == "max_candles"
    assert rows[0]["extended_final_status"] == "closed"
    assert rows[0]["resolved_with_extended_window"] == "True"


def test_select_unfinished_runs_by_start_indices() -> None:
    results = [
        BacktestResult(symbol="X", direction="long", start_index=0, final_status="closed"),
        BacktestResult(symbol="X", direction="long", start_index=10, final_status="max_candles"),
        BacktestResult(symbol="X", direction="long", start_index=20, final_status="open"),
    ]
    selected = select_unfinished_runs_for_deep_dive(results, start_indices={0, 10})
    assert len(selected) == 1
    assert selected[0].start_index == 10


def test_parse_deep_dive_start_indices() -> None:
    assert parse_deep_dive_start_indices("800, 1300,1600") == {800, 1300, 1600}
    assert parse_deep_dive_start_indices(None) is None


def _flat_candles(n: int) -> list[SyntheticCandle]:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        SyntheticCandle(
            symbol="BTCUSDT",
            timestamp=base,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
        )
        for _ in range(n)
    ]


def test_cli_multi_start_deep_dive_writes_outputs(tmp_path: Path) -> None:
    candles = [
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
        }
        for _ in range(40)
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
                "40",
                "--multi-start",
                "--deep-dive-unfinished",
                "--window-candles",
                "10",
                "--extended-window-candles",
                "20",
                "--start-step-candles",
                "5",
                "--max-starts",
                "3",
                "--config-source",
                "test",
                "--output-dir",
                str(tmp_path),
            ]
        )
    assert exit_code == 0
    csv_path, json_path = deep_dive_output_paths(tmp_path, "BTCUSDT")
    assert csv_path.exists()
    assert json_path.exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert "deep_dive_runs" in payload
    assert payload["metadata"]["extended_window_candles"] == 20


def test_deep_dive_start_indices_filter_in_cli(tmp_path: Path) -> None:
    candles = [
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
        }
        for _ in range(40)
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
                "40",
                "--multi-start",
                "--deep-dive-unfinished",
                "--deep-dive-start-indices",
                "5",
                "--window-candles",
                "10",
                "--extended-window-candles",
                "20",
                "--start-step-candles",
                "5",
                "--max-starts",
                "3",
                "--config-source",
                "test",
                "--output-dir",
                str(tmp_path),
                "--no-json",
            ]
        )
    assert exit_code == 0
    csv_path, _ = deep_dive_output_paths(tmp_path, "BTCUSDT")
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert all(row["start_index"] == "5" for row in rows)


def test_run_unfinished_deep_dive_after_multi_start_with_synthetic_results(tmp_path: Path) -> None:
    candles = _flat_candles(30)
    multi_payload = {
        "symbol": "BTCUSDT",
        "directions": ["long"],
        "aggregate": [],
        "results": [
            BacktestResult(
                symbol="BTCUSDT",
                direction="long",
                start_index=0,
                final_status="max_candles",
                exit_reason="max_candles_reached",
                open_reason_detail="max_candles_reached",
                realized_pnl=0.0,
                fills_count=2,
                candles_processed=9,
            )
        ],
    }
    with patch(
        "research.backtests.unfinished_deep_dive._run_backtest_window",
        side_effect=lambda **kwargs: BacktestResult(
            symbol="BTCUSDT",
            direction="long",
            start_index=kwargs["start_index"],
            final_status="closed",
            exit_reason="flat_no_active_orders",
            open_reason_detail="closed",
            realized_pnl=0.5,
            fills_count=3,
            candles_processed=19,
        ),
    ):
        payload = run_unfinished_deep_dive_after_multi_start(
            multi_start_payload=multi_payload,
            candles=candles,
            config_source="test",
            fill_model="conservative",
            max_fills_per_candle=None,
            original_window_candles=10,
            extended_window_candles=20,
            output_dir=tmp_path,
        )
    assert payload["resolved_count"] == 1
    assert payload["rows"][0]["additional_candles_needed"] == 10


@pytest.mark.skipif(
    not (DEFAULT_DATA_DIR / symbol_to_feather_name("APTUSDT")).exists(),
    reason="APT feather file not available",
)
def test_apt_deep_dive_smoke(tmp_path: Path) -> None:
    exit_code = cli_main(
        [
            "--symbol",
            "APTUSDT",
            "--direction",
            "long",
            "--limit",
            "2500",
            "--multi-start",
            "--deep-dive-unfinished",
            "--window-candles",
            "500",
            "--extended-window-candles",
            "1000",
            "--start-step-candles",
            "100",
            "--max-starts",
            "3",
            "--config-source",
            "live",
            "--output-dir",
            str(tmp_path),
        ]
    )
    assert exit_code == 0
    csv_path, json_path = deep_dive_output_paths(tmp_path, "APTUSDT")
    assert csv_path.exists()
    assert json_path.exists()

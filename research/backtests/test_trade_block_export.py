"""Phase-14 trade block export tests."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from research.backtests.backtest_report import BacktestResult
from research.backtests.candle_loader import DEFAULT_DATA_DIR, symbol_to_feather_name
from research.backtests.run_original_hedge_backtest import main as cli_main
from research.backtests.trade_block_export import (
    TRADE_BLOCK_ROW_FIELDS,
    build_trade_block_rows,
    build_trade_block_summary_rows,
    export_trade_blocks_for_results,
    parse_trade_block_start_indices,
    sort_trade_block_rows,
    write_trade_block_exports,
)


def _result_with_logs(*, trade_block_id: str | None = "tb-123") -> BacktestResult:
    metadata = {"trade_block_id": trade_block_id} if trade_block_id else {}
    return BacktestResult(
        symbol="APTUSDT",
        direction="long",
        start_index=0,
        config_source="live",
        fill_model="conservative",
        fill_log=[
            {
                "timestamp": "2026-01-01T00:05:00+00:00",
                "candle_index": 1,
                "purpose": "INITIAL_LONG_ENTRY",
                "purpose_original": "INITIAL_LONG_ENTRY",
                "side": "long",
                "qty": 100.0,
                "fill_price": 1.0,
                "closed_pnl": 0.0,
                **metadata,
            },
            {
                "timestamp": "2026-01-01T00:10:00+00:00",
                "candle_index": 2,
                "purpose": "CYCLE_1_SHORT_REDUCE",
                "purpose_original": "CYCLE_1_SHORT_REDUCE",
                "side": "short",
                "qty": 25.0,
                "fill_price": 1.01,
                "closed_pnl": 1.0,
                "cycle_index": 1,
                "cycle_role": "short_reduce",
                **metadata,
            },
        ],
        order_log=[
            {
                "timestamp": "2026-01-01T00:04:00+00:00",
                "candle_index": 1,
                "event_type": "submitted",
                "purpose": "LONG_SL_EXIT",
                "purpose_original": "LONG_SL_EXIT",
                "order_id": "ord-1",
                **metadata,
            }
        ],
        intent_log=[
            {
                "timestamp": "2026-01-01T00:03:00+00:00",
                "candle_index": 1,
                "event_source": "entry",
                "purpose": "INITIAL_LONG_ENTRY",
                "purpose_original": "INITIAL_LONG_ENTRY",
                "metadata_excerpt": {"trade_block_id": trade_block_id} if trade_block_id else {},
            }
        ],
    )


def test_build_trade_block_rows_contains_trade_block_id_and_purpose() -> None:
    rows = build_trade_block_rows(_result_with_logs())
    assert rows
    assert all(row["trade_block_id"] == "tb-123" for row in rows)
    assert all(row["trade_block_id_missing"] is False for row in rows)
    purposes = {row["purpose"] for row in rows}
    assert "INITIAL_LONG_ENTRY" in purposes
    assert "CYCLE_1_SHORT_REDUCE" in purposes
    assert "LONG_SL_EXIT" in purposes


def test_missing_trade_block_id_uses_fallback() -> None:
    result = _result_with_logs(trade_block_id=None)
    rows = build_trade_block_rows(result)
    assert rows
    assert rows[0]["trade_block_id"] == "backtest_long_start0"
    assert rows[0]["trade_block_id_missing"] is True


def test_cumulative_pnl_on_fills() -> None:
    result = BacktestResult(
        symbol="APTUSDT",
        direction="long",
        fill_log=[
            {
                "timestamp": "2026-01-01T00:05:00+00:00",
                "purpose": "FILL_A",
                "closed_pnl": 1.0,
                "trade_block_id": "tb-1",
            },
            {
                "timestamp": "2026-01-01T00:10:00+00:00",
                "purpose": "FILL_B",
                "closed_pnl": -0.25,
                "trade_block_id": "tb-1",
            },
        ],
    )
    fills = [row for row in build_trade_block_rows(result) if row["row_type"] == "fill"]
    assert fills[0]["cumulative_pnl"] == pytest.approx(1.0)
    assert fills[1]["cumulative_pnl"] == pytest.approx(0.75)


def test_build_trade_block_summary_rows() -> None:
    summaries = build_trade_block_summary_rows(_result_with_logs())
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary["trade_block_id"] == "tb-123"
    assert summary["fills_count"] == 2
    assert summary["orders_count"] == 1
    assert summary["intents_count"] == 1
    assert "INITIAL_LONG_ENTRY" in summary["purposes_sequence"]
    assert summary["cycle_indices"] == "1"


def test_sort_trade_block_rows_row_type_order() -> None:
    rows = sort_trade_block_rows(
        [
            {"trade_block_id": "tb", "timestamp": "t", "candle_index": 1, "row_type": "fill"},
            {"trade_block_id": "tb", "timestamp": "t", "candle_index": 1, "row_type": "intent"},
            {"trade_block_id": "tb", "timestamp": "t", "candle_index": 1, "row_type": "order"},
        ]
    )
    assert [row["row_type"] for row in rows] == ["intent", "order", "fill"]


def test_write_trade_block_exports(tmp_path: Path) -> None:
    result = _result_with_logs()
    files = write_trade_block_exports(result, tmp_path)
    assert Path(files["trade_blocks_csv"]).exists()
    assert Path(files["trade_block_summary_csv"]).exists()
    assert Path(files["trade_blocks_json"]).exists()

    with Path(files["trade_blocks_csv"]).open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert list(rows[0].keys()) == list(TRADE_BLOCK_ROW_FIELDS)
    assert rows[0]["purpose"]

    payload = json.loads(Path(files["trade_blocks_json"]).read_text(encoding="utf-8"))
    assert payload["metadata"]["symbol"] == "APTUSDT"
    assert payload["trade_blocks"]


def test_cli_single_trade_block_export(tmp_path: Path) -> None:
    candles = [
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
        }
        for _ in range(10)
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
                "10",
                "--config-source",
                "test",
                "--trade-block-export",
                "--output-dir",
                str(tmp_path),
                "--no-json",
                "--no-csv",
            ]
        )
    assert exit_code == 0
    exported = list(tmp_path.glob("BTCUSDT_long_start0_*_trade_blocks.csv"))
    assert exported


def test_cli_multi_trade_block_export_selected_start(tmp_path: Path) -> None:
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
                "--start-step-candles",
                "5",
                "--window-candles",
                "10",
                "--max-starts",
                "3",
                "--config-source",
                "test",
                "--trade-block-export",
                "--trade-block-start-indices",
                "0",
                "--output-dir",
                str(tmp_path),
                "--no-json",
                "--no-csv",
            ]
        )
    assert exit_code == 0
    assert list(tmp_path.glob("BTCUSDT_long_start0_*_trade_blocks.csv"))
    assert not list(tmp_path.glob("BTCUSDT_long_start5_*_trade_blocks.csv"))


def test_export_trade_blocks_for_results_filters_start_indices(tmp_path: Path) -> None:
    results = [
        BacktestResult(symbol="X", direction="long", start_index=0, fill_log=[]),
        BacktestResult(symbol="X", direction="long", start_index=5, fill_log=[]),
    ]
    written = export_trade_blocks_for_results(
        results,
        tmp_path,
        start_indices={0},
    )
    assert len(written) == 1


def test_parse_trade_block_start_indices() -> None:
    assert parse_trade_block_start_indices("800,1600") == {800, 1600}


@pytest.mark.skipif(
    not (DEFAULT_DATA_DIR / symbol_to_feather_name("APTUSDT")).exists(),
    reason="APT feather file not available",
)
def test_apt_trade_block_export_smoke(tmp_path: Path) -> None:
    exit_code = cli_main(
        [
            "--symbol",
            "APTUSDT",
            "--direction",
            "long",
            "--limit",
            "1000",
            "--config-source",
            "live",
            "--trade-block-export",
            "--output-dir",
            str(tmp_path),
            "--no-json",
            "--no-csv",
        ]
    )
    assert exit_code == 0
    assert list(tmp_path.glob("APTUSDT_long_start0_*_trade_blocks.csv"))

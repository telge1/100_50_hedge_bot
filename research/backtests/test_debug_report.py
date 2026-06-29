"""Phase-6 debug report tests."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from research.backtests.backtest_report import BacktestResult, SUMMARY_CSV_FIELDS, result_to_summary_row
from research.backtests.debug_report import explain_open_reason, print_debug_report
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.run_original_hedge_backtest import main as cli_main, run_original_hedge_backtests
from research.backtests.simulated_order_book import SyntheticCandle


def _mini_candles(count: int = 5) -> list[SyntheticCandle]:
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
        for _ in range(count)
    ]


def test_historical_backtest_to_dict_contains_debug_fields() -> None:
    result = run_historical_backtest("BTCUSDT", "long", _mini_candles(5), max_candles=3)
    payload = result.to_dict()

    assert "final_active_orders" in payload
    assert "final_long_qty" in payload
    assert "final_short_qty" in payload
    assert "open_reason_detail" in payload
    assert payload["open_reason_detail"]
    assert payload["fills_count"] >= 2


def test_explain_open_reason_waiting_for_order_fill() -> None:
    result = BacktestResult(
        symbol="BTCUSDT",
        direction="long",
        final_status="open",
        exit_reason="series_end_with_open_positions",
        final_long_qty=1.0,
        final_short_qty=0.5,
        final_active_orders=[
            {
                "purpose": "LONG_TP_EXIT",
                "side": "long",
                "qty": 1.0,
                "price": None,
                "trigger_price": 101.3,
            }
        ],
    )
    reason = explain_open_reason(result)
    assert reason.startswith("waiting_for_order_fill:LONG_TP_EXIT")
    assert "trigger=101.3" in reason


def test_explain_open_reason_open_long_position() -> None:
    result = BacktestResult(
        symbol="BTCUSDT",
        direction="long",
        final_status="open",
        final_long_qty=1.0,
        final_short_qty=0.0,
        final_active_orders=[],
    )
    reason = explain_open_reason(result)
    assert "open_long_position" in reason


def test_explain_open_reason_flat_but_active_orders() -> None:
    result = BacktestResult(
        symbol="BTCUSDT",
        direction="long",
        final_status="open",
        final_long_qty=0.0,
        final_short_qty=0.0,
        final_active_orders=[
            {
                "purpose": "CYCLE_1_SHORT_REDUCE",
                "price": 99.0,
                "trigger_price": 99.0,
            }
        ],
    )
    reason = explain_open_reason(result)
    assert reason.startswith("flat_but_active_orders|")


def test_cli_debug_writes_json_with_debug_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    candles = _mini_candles(6)

    def _fake_load(*args, **kwargs):
        return [
            {
                "timestamp": candle.timestamp,
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
            }
            for candle in candles
        ]

    monkeypatch.setattr(
        "research.backtests.run_original_hedge_backtest.load_candles_for_symbol",
        _fake_load,
    )

    output = StringIO()
    with patch("sys.stdout", output):
        exit_code = cli_main(
            [
                "--symbol",
                "BTCUSDT",
                "--direction",
                "long",
                "--limit",
                "6",
                "--max-candles",
                "4",
                "--output-dir",
                str(tmp_path),
                "--debug",
            ]
        )
    assert exit_code == 0
    assert "--- debug long ---" in output.getvalue()

    json_payload = json.loads((tmp_path / "BTCUSDT_original_hedge_5m_results.json").read_text())
    run = json_payload["runs"]["long"]
    assert run["open_reason_detail"]
    assert "final_active_orders" in run

    with (tmp_path / "BTCUSDT_original_hedge_5m_summary.csv").open("r", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["open_reason_detail"]
    assert set(SUMMARY_CSV_FIELDS) <= set(rows[0].keys())


def test_summary_row_formats_active_order_purposes() -> None:
    result = BacktestResult(
        symbol="APTUSDT",
        direction="long",
        final_active_order_purposes=["LONG_TP_EXIT", "CYCLE_1_SHORT_REDUCE"],
        open_reason_detail="waiting_for_order_fill:LONG_TP_EXIT price=None trigger=101.3",
    )
    row = result_to_summary_row(result)
    assert row["final_active_order_purposes"] == "LONG_TP_EXIT|CYCLE_1_SHORT_REDUCE"


def test_print_debug_report_no_exception(capsys: pytest.CaptureFixture[str]) -> None:
    result = BacktestResult(
        symbol="BTCUSDT",
        direction="long",
        final_status="open",
        open_reason_detail="waiting_for_order_fill:LONG_TP_EXIT price=None trigger=101.3",
        fill_log=[
            {
                "timestamp": "t",
                "purpose": "INITIAL_LONG_ENTRY",
                "side": "long",
                "qty": 1,
                "fill_price": 100,
                "closed_pnl": 0,
            }
        ],
        order_log=[
            {
                "timestamp": "t",
                "event_type": "submitted",
                "purpose": "LONG_TP_EXIT",
                "side": "long",
                "qty": 1,
                "price": None,
                "trigger_price": 101.3,
                "status": "NEW",
            }
        ],
    )
    print_debug_report(result)
    captured = capsys.readouterr().out
    assert "open_reason_detail=" in captured
    assert "last_1_fills" in captured


def test_apt_debug_runner_if_data_available(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    from research.backtests.candle_loader import DEFAULT_DATA_DIR, symbol_to_feather_name

    apt_path = DEFAULT_DATA_DIR / symbol_to_feather_name("APTUSDT")
    if not apt_path.exists():
        pytest.skip(f"external APT feather file not present: {apt_path}")

    payload = run_original_hedge_backtests(
        symbol="APTUSDT",
        direction="both",
        limit=500,
        max_candles=500,
        output_dir=tmp_path,
    )
    for direction in ("long", "short"):
        result = payload["results"][direction]
        assert result.error is None
        assert result.open_reason_detail
        assert "final_active_orders" in result.to_dict()

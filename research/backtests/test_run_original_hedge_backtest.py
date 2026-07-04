"""Phase-5 CLI runner and report writer tests."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from research.backtests.backtest_report import (
    BacktestResult,
    SUMMARY_CSV_FIELDS,
    default_output_paths,
    result_to_summary_row,
    write_results_json,
    write_summary_csv,
)
from research.backtests.candle_loader import DEFAULT_DATA_DIR, symbol_to_feather_name
from research.backtests.run_original_hedge_backtest import (
    run_original_hedge_backtests,
    _build_parser,
    main as cli_main,
)
from research.backtests.simulated_order_book import SyntheticCandle


def _synthetic_candles(n: int = 5) -> list[SyntheticCandle]:
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


def test_summary_writer_from_backtest_result(tmp_path: Path) -> None:
    result = BacktestResult(
        symbol="BTCUSDT",
        direction="long",
        start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end_time=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        candles_processed=3,
        entry_price=100.0,
        final_status="open",
        exit_reason="series_end_with_open_positions",
        realized_pnl=1.25,
        realized_pnl_pct=1.25,
        max_drawdown_pct=0.5,
        fills_count=2,
        orders_submitted=2,
        active_orders_count=1,
        cycles_seen=1,
        fill_log=[{"purpose": "INITIAL_LONG_ENTRY"}],
        order_log=[{"purpose": "LONG_TP_EXIT"}],
    )

    csv_path = tmp_path / "summary.csv"
    write_summary_csv(csv_path, [result])
    assert csv_path.exists()

    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)

    assert reader.fieldnames == list(SUMMARY_CSV_FIELDS)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTCUSDT"
    assert rows[0]["direction"] == "long"
    assert float(rows[0]["realized_pnl"]) == pytest.approx(1.25)

    json_path = tmp_path / "results.json"
    write_results_json(
        json_path,
        symbol="BTCUSDT",
        limit=5,
        max_candles=4,
        results={"long": result},
    )
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["symbol"] == "BTCUSDT"
    assert "long" in payload["runs"]
    assert payload["runs"]["long"]["fill_log"]
    assert payload["runs"]["long"]["order_log"]


def test_run_original_hedge_backtests_writes_csv_json_with_synthetic_candles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candles = _synthetic_candles(6)

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

    payload = run_original_hedge_backtests(
        symbol="BTCUSDT",
        direction="long",
        limit=6,
        max_candles=4,
        output_dir=tmp_path,
    )

    json_path = Path(payload["output_files"]["json"])
    csv_path = Path(payload["output_files"]["csv"])
    assert json_path.exists()
    assert csv_path.exists()

    json_payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert json_payload["runs"]["long"]["fills_count"] >= 2

    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["direction"] == "long"


def test_default_output_paths_naming() -> None:
    json_path, csv_path = default_output_paths("research/backtests/results", "APTUSDT")
    assert json_path.name == "APTUSDT_original_hedge_5m_results.json"
    assert csv_path.name == "APTUSDT_original_hedge_5m_summary.csv"


def test_result_to_summary_row_omits_fill_log() -> None:
    result = BacktestResult(
        symbol="APTUSDT",
        direction="short",
        fill_log=[{"purpose": "INITIAL_SHORT_ENTRY"}],
    )
    row = result_to_summary_row(result)
    assert set(row.keys()) == set(SUMMARY_CSV_FIELDS)
    assert "fill_log" not in row


def test_aptusdt_runner_with_real_data_if_available(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    apt_path = DEFAULT_DATA_DIR / symbol_to_feather_name("APTUSDT")
    if not apt_path.exists():
        pytest.skip(f"external APT feather file not present: {apt_path}")

    payload = run_original_hedge_backtests(
        symbol="APTUSDT",
        direction="long",
        limit=200,
        max_candles=200,
        output_dir=tmp_path,
    )

    assert payload["candles_loaded"] == 200
    assert Path(payload["output_files"]["json"]).exists()
    assert Path(payload["output_files"]["csv"]).exists()

    with Path(payload["output_files"]["csv"]).open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) >= 1
    assert rows[0]["symbol"] == "APTUSDT"

    json_payload = json.loads(Path(payload["output_files"]["json"]).read_text(encoding="utf-8"))
    assert "long" in json_payload["runs"]
    assert json_payload["runs"]["long"]["error"] is None


def test_parser_recognizes_use_live_short_tp_relief_flag() -> None:
    parser = _build_parser()
    args = parser.parse_args(["--use-live-short-tp-relief"])
    assert getattr(args, "use_live_short_tp_relief", False) is True


def test_run_original_hedge_backtests_forwards_use_live_short_tp_relief(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    def _fake_run_historical_backtest(*args, **kwargs):
        calls.update(kwargs)
        # Minimal BacktestResult stub for this unit test
        return BacktestResult(symbol="BTCUSDT", direction="long")

    monkeypatch.setattr(
        "research.backtests.run_original_hedge_backtest.run_historical_backtest",
        _fake_run_historical_backtest,
    )

    run_original_hedge_backtests(
        symbol="BTCUSDT",
        direction="long",
        limit=10,
        max_candles=5,
        output_dir=".",
        write_json=False,
        write_csv=False,
        candles=[{"timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc), "open": 1, "high": 1, "low": 1, "close": 1}],
        use_live_short_tp_relief=True,
    )

    assert calls.get("use_live_short_tp_relief") is True


def test_cli_rejects_conflicting_live_and_shim_relief_flags(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli_main(
        [
            "--symbol",
            "APTUSDT",
            "--direction",
            "long",
            "--limit",
            "10",
            "--max-candles",
            "10",
            "--use-live-short-tp-relief",
            "--cycle-short-tp-relief",
            "--no-json",
            "--no-csv",
        ]
    )
    captured = capsys.readouterr()
    assert code == 1
    assert "cannot be combined" in captured.err

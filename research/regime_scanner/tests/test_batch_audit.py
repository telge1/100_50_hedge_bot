"""Tests for the blocker batch regime audit."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd
import pytest

from research.backtests.candle_loader import DEFAULT_DATA_DIR, symbol_to_feather_name
from research.regime_scanner.batch_audit import (
    DECISION_TIME_COLUMN,
    audit_one_trade,
    build_batch_summary,
    extract_trade_row,
    load_blocker_csv,
    run_batch_audit,
    write_batch_outputs,
)
from research.regime_scanner.point_audit import json_safe


def _synth_candles(n: int = 400, start: str = "2025-12-01T00:00:00+00:00") -> pd.DataFrame:
    start_ts = pd.Timestamp(start)
    rows = []
    for i in range(n):
        ts = start_ts + pd.Timedelta(minutes=5 * i)
        px = 1.5 + (i % 50) * 0.01 + math.sin(i / 9.0) * 0.05
        rows.append(
            {
                "timestamp": ts,
                "open": px,
                "high": px + 0.02,
                "low": px - 0.02,
                "close": px + 0.005,
                "volume": 10.0 + i % 5,
            }
        )
    return pd.DataFrame(rows)


def _write_csv(path: Path, rows: list[dict]) -> None:
    pd.DataFrame(rows).to_csv(path, index=False)


def test_csv_loaded_and_decision_column_used(tmp_path: Path) -> None:
    csv_path = tmp_path / "in.csv"
    _write_csv(
        csv_path,
        [
            {
                "trade_id": "t1",
                "start_index": 10,
                "category": "negative_closed",
                "pnl": -1.5,
                "status": "closed_negative_pnl",
                "start_candle_open_utc": "2025-12-28T16:15:00+00:00",
                DECISION_TIME_COLUMN: "2025-12-28T16:20:00+00:00",
            }
        ],
    )
    frame = load_blocker_csv(csv_path)
    assert DECISION_TIME_COLUMN in frame.columns
    assert frame.iloc[0][DECISION_TIME_COLUMN] == "2025-12-28T16:20:00+00:00"


def test_missing_timestamp_becomes_error_row_not_crash(tmp_path: Path) -> None:
    csv_path = tmp_path / "in.csv"
    _write_csv(
        csv_path,
        [
            {
                "trade_id": "bad",
                "start_index": 1,
                "category": "negative_closed",
                "pnl": -0.1,
                "status": "closed_negative_pnl",
                "start_candle_open_utc": "2025-12-28T16:15:00+00:00",
                DECISION_TIME_COLUMN: None,
            },
            {
                "trade_id": "ok",
                "start_index": 20,
                "category": "negative_closed",
                "pnl": -0.2,
                "status": "closed_negative_pnl",
                "start_candle_open_utc": "2025-12-01T02:00:00+00:00",
                DECISION_TIME_COLUMN: "2025-12-01T02:05:00+00:00",
            },
        ],
    )
    candles = _synth_candles()
    payload = run_batch_audit(
        input_csv=csv_path,
        symbol="SYN",
        timeframes="5m,15m,30m",
        candles=candles,
    )
    assert payload["summary"]["trade_count"] == 2
    assert payload["summary"]["errors"] >= 1
    assert payload["summary"]["successes"] >= 1
    bad = next(r for r in payload["rows"] if r["trade_id"] == "bad")
    assert bad["status"] == "error"
    assert bad["combined_regime"] == "unavailable"


def test_single_analysis_returns_regime(tmp_path: Path) -> None:
    candles = _synth_candles(500)
    decision = candles["timestamp"].iloc[300] + pd.Timedelta(minutes=5)
    row = audit_one_trade(
        input_row={
            "trade_id": "t1",
            "start_index": 300,
            "category": "negative_closed",
            "pnl": -1.0,
            "status": "closed_negative_pnl",
            "start_candle_open_utc": str(candles["timestamp"].iloc[300]),
            DECISION_TIME_COLUMN: decision.isoformat(),
        },
        symbol="SYN",
        timeframes="5m,15m,30m",
        history_candles=144,
        candles=candles,
    )
    assert row["status"] == "success"
    assert row["combined_regime"] in {
        "strong_bullish_trend",
        "bullish_trend",
        "bullish_trend_with_trend_weakness",
        "neutral",
        "transition",
        "bearish_trend",
        "bearish_trend_with_trend_weakness",
        "strong_bearish_trend",
        "unavailable",
    }
    assert row["last_closed_candle_5m"] is not None


def test_rows_sorted_by_pnl_ascending(tmp_path: Path) -> None:
    csv_path = tmp_path / "in.csv"
    candles = _synth_candles(500)
    d1 = (candles["timestamp"].iloc[200] + pd.Timedelta(minutes=5)).isoformat()
    d2 = (candles["timestamp"].iloc[250] + pd.Timedelta(minutes=5)).isoformat()
    d3 = (candles["timestamp"].iloc[300] + pd.Timedelta(minutes=5)).isoformat()
    _write_csv(
        csv_path,
        [
            {"trade_id": "a", "start_index": 200, "category": "negative_closed", "pnl": -1.0, "status": "x", "start_candle_open_utc": str(candles["timestamp"].iloc[200]), DECISION_TIME_COLUMN: d1},
            {"trade_id": "b", "start_index": 250, "category": "negative_closed", "pnl": -5.0, "status": "x", "start_candle_open_utc": str(candles["timestamp"].iloc[250]), DECISION_TIME_COLUMN: d2},
            {"trade_id": "c", "start_index": 300, "category": "negative_closed", "pnl": -2.0, "status": "x", "start_candle_open_utc": str(candles["timestamp"].iloc[300]), DECISION_TIME_COLUMN: d3},
        ],
    )
    payload = run_batch_audit(input_csv=csv_path, symbol="SYN", candles=candles)
    pnls = [r["pnl"] for r in payload["rows"]]
    assert pnls == sorted(pnls)
    assert payload["rows"][0]["trade_id"] == "b"


def test_summary_counts_regimes_and_pnl() -> None:
    rows = [
        {"trade_id": "1", "status": "success", "combined_regime": "bullish_trend_with_trend_weakness", "pnl": -10.0, "category": "negative_closed", "multi_timeframe_trend_weakness": True, "developing_equal_high_exhaustion_15m": True, "confirmed_equal_high_exhaustion_15m": False, "multi_metric_equal_high_exhaustion_15m": True, "last_bar_rollover_signals_5m": ["PLUS_DI_LAST_BAR_ROLLOVER"], "last_bar_rollover_signals_15m": [], "last_bar_rollover_signals_30m": [], "developing_equal_high_exhaustion_5m": False, "developing_equal_high_exhaustion_30m": False, "confirmed_equal_high_exhaustion_5m": False, "confirmed_equal_high_exhaustion_30m": False, "multi_metric_equal_high_exhaustion_5m": False, "multi_metric_equal_high_exhaustion_30m": False},
        {"trade_id": "2", "status": "success", "combined_regime": "bullish_trend_with_trend_weakness", "pnl": -2.0, "category": "negative_closed", "multi_timeframe_trend_weakness": False, "developing_equal_high_exhaustion_15m": False, "confirmed_equal_high_exhaustion_15m": True, "multi_metric_equal_high_exhaustion_15m": False, "last_bar_rollover_signals_5m": [], "last_bar_rollover_signals_15m": [], "last_bar_rollover_signals_30m": [], "developing_equal_high_exhaustion_5m": False, "developing_equal_high_exhaustion_30m": False, "confirmed_equal_high_exhaustion_5m": False, "confirmed_equal_high_exhaustion_30m": False, "multi_metric_equal_high_exhaustion_5m": False, "multi_metric_equal_high_exhaustion_30m": False},
        {"trade_id": "3", "status": "success", "combined_regime": "neutral", "pnl": -4.0, "category": "negative_closed", "multi_timeframe_trend_weakness": False, "developing_equal_high_exhaustion_15m": False, "confirmed_equal_high_exhaustion_15m": False, "multi_metric_equal_high_exhaustion_15m": False, "last_bar_rollover_signals_5m": [], "last_bar_rollover_signals_15m": [], "last_bar_rollover_signals_30m": [], "developing_equal_high_exhaustion_5m": False, "developing_equal_high_exhaustion_30m": False, "confirmed_equal_high_exhaustion_5m": False, "confirmed_equal_high_exhaustion_30m": False, "multi_metric_equal_high_exhaustion_5m": False, "multi_metric_equal_high_exhaustion_30m": False},
        {"trade_id": "4", "status": "error", "combined_regime": "unavailable", "pnl": -1.0, "category": "negative_closed", "error_message": "boom", "multi_timeframe_trend_weakness": False, "developing_equal_high_exhaustion_15m": False, "confirmed_equal_high_exhaustion_15m": False, "multi_metric_equal_high_exhaustion_15m": False, "last_bar_rollover_signals_5m": [], "last_bar_rollover_signals_15m": [], "last_bar_rollover_signals_30m": [], "developing_equal_high_exhaustion_5m": False, "developing_equal_high_exhaustion_30m": False, "confirmed_equal_high_exhaustion_5m": False, "confirmed_equal_high_exhaustion_30m": False, "multi_metric_equal_high_exhaustion_5m": False, "multi_metric_equal_high_exhaustion_30m": False},
    ]
    summary = build_batch_summary(rows)
    assert summary["successes"] == 3
    assert summary["errors"] == 1
    weak = summary["pnl_by_combined_regime"]["bullish_trend_with_trend_weakness"]
    assert weak["count"] == 2
    assert weak["pnl_sum"] == pytest.approx(-12.0)
    assert weak["pnl_avg"] == pytest.approx(-6.0)
    assert weak["pnl_median"] == pytest.approx(-6.0)
    assert weak["share_of_negative_trades"] == pytest.approx(2 / 4)
    assert weak["share_of_total_loss"] == pytest.approx((-12.0) / (-17.0))
    assert summary["named_regime_counts"]["bullish_trend_with_trend_weakness"] == 2
    assert summary["developing_equal_high_exhaustion_counts"]["15m"] == 1
    assert summary["confirmed_equal_high_exhaustion_counts"]["15m"] == 1
    assert summary["multi_metric_equal_high_exhaustion_counts"]["15m"] == 1
    assert summary["last_bar_rollover_counts"]["5m"] == 1
    assert summary["multi_timeframe_trend_weakness_count"] == 1


def test_json_serializable_and_no_infinity(tmp_path: Path) -> None:
    csv_path = tmp_path / "in.csv"
    candles = _synth_candles(400)
    decision = (candles["timestamp"].iloc[250] + pd.Timedelta(minutes=5)).isoformat()
    _write_csv(
        csv_path,
        [
            {
                "trade_id": "t1",
                "start_index": 250,
                "category": "negative_closed",
                "pnl": -0.5,
                "status": "x",
                "start_candle_open_utc": str(candles["timestamp"].iloc[250]),
                DECISION_TIME_COLUMN: decision,
            }
        ],
    )
    payload = run_batch_audit(input_csv=csv_path, symbol="SYN", candles=candles)
    safe = json_safe(payload)
    encoded = json.dumps(safe, allow_nan=False)
    assert "Infinity" not in encoded
    assert "NaN" not in encoded
    paths = write_batch_outputs(payload, tmp_path / "out")
    assert paths["csv"].exists()
    assert paths["summary_md"].exists()


def test_deterministic_same_inputs(tmp_path: Path) -> None:
    csv_path = tmp_path / "in.csv"
    candles = _synth_candles(400)
    decision = (candles["timestamp"].iloc[250] + pd.Timedelta(minutes=5)).isoformat()
    _write_csv(
        csv_path,
        [
            {
                "trade_id": "t1",
                "start_index": 250,
                "category": "negative_closed",
                "pnl": -0.5,
                "status": "x",
                "start_candle_open_utc": str(candles["timestamp"].iloc[250]),
                DECISION_TIME_COLUMN: decision,
            }
        ],
    )
    a = run_batch_audit(input_csv=csv_path, symbol="SYN", candles=candles)
    b = run_batch_audit(input_csv=csv_path, symbol="SYN", candles=candles.copy())
    assert json_safe(a["rows"][0]["combined_regime"]) == json_safe(b["rows"][0]["combined_regime"])
    assert json_safe(a["summary"]["pnl_sum"]) == json_safe(b["summary"]["pnl_sum"])


def test_future_mutation_does_not_change_batch_row() -> None:
    candles = _synth_candles(400)
    decision_ts = candles["timestamp"].iloc[250] + pd.Timedelta(minutes=5)
    # Append future bars after decision.
    future_rows = []
    for i in range(12):
        ts = decision_ts + pd.Timedelta(minutes=5 * i)
        future_rows.append(
            {
                "timestamp": ts,
                "open": 999.0,
                "high": 1000.0,
                "low": 998.0,
                "close": 999.5,
                "volume": 999.0,
            }
        )
    with_future = pd.concat([candles, pd.DataFrame(future_rows)], ignore_index=True)
    mutated = with_future.copy()
    mutated.loc[mutated["timestamp"] >= decision_ts, ["high", "close"]] = 1e6
    input_row = {
        "trade_id": "t1",
        "start_index": 250,
        "category": "negative_closed",
        "pnl": -1.0,
        "status": "x",
        "start_candle_open_utc": str(candles["timestamp"].iloc[250]),
        DECISION_TIME_COLUMN: decision_ts.isoformat(),
    }
    a = audit_one_trade(
        input_row=input_row,
        symbol="SYN",
        timeframes="5m,15m,30m",
        history_candles=144,
        candles=with_future,
    )
    b = audit_one_trade(
        input_row=input_row,
        symbol="SYN",
        timeframes="5m,15m,30m",
        history_candles=144,
        candles=mutated,
    )
    assert a["combined_regime"] == b["combined_regime"]
    assert a["adx_15m"] == b["adx_15m"]
    assert a["last_closed_candle_5m"] == b["last_closed_candle_5m"]
    assert pd.Timestamp(a["last_closed_candle_5m"]) < decision_ts


def test_extract_uses_decision_not_start_open() -> None:
    row = extract_trade_row(
        input_row={
            "trade_id": "t",
            "start_index": 1,
            "start_candle_open_utc": "2026-01-13T22:55:00+00:00",
            DECISION_TIME_COLUMN: "2026-01-13T23:00:00+00:00",
            "category": "negative_closed",
            "pnl": -1.0,
            "status": "closed_negative_pnl",
        },
        audit=None,
        error="forced",
    )
    assert row["decision_time_after_close_utc"] == "2026-01-13T23:00:00+00:00"
    assert row["start_candle_open_utc"] == "2026-01-13T22:55:00+00:00"


@pytest.mark.skipif(
    not (DEFAULT_DATA_DIR / symbol_to_feather_name("APTUSDT")).exists(),
    reason="external APT feather file not present",
)
def test_real_csv_runs_32_trades() -> None:
    csv_path = Path("research/backtests/results/aptusdt_blocker_start_times_resolved.csv")
    assert csv_path.exists()
    payload = run_batch_audit(
        input_csv=csv_path,
        symbol="APTUSDT",
        timeframes="5m,15m,30m",
        history_candles=144,
    )
    assert payload["summary"]["trade_count"] == 32
    assert payload["summary"]["successes"] + payload["summary"]["errors"] == 32
    # Prefer all success on real data when decision times are resolved.
    assert payload["summary"]["successes"] >= 30


def test_positive_closed_extracted_excludes_negative_and_open(tmp_path: Path) -> None:
    from research.regime_scanner.trade_list_builder import extract_trades_from_result_file

    result = {
        "metadata": {},
        "aggregate": [{"total_pnl": 1.0, "successful_closed_count": 2}],
        "runs": [
            {
                "trade_block_id": "w1",
                "final_status": "closed",
                "overall_pnl": 1.5,
                "start_index": 10,
                "input_slice_start_index": 0,
                "start_time": "2025-12-01T00:50:00+00:00",
            },
            {
                "trade_block_id": "w1",
                "final_status": "closed",
                "overall_pnl": 9.9,
                "start_index": 11,
                "input_slice_start_index": 0,
                "start_time": "2025-12-01T00:55:00+00:00",
            },
            {
                "trade_block_id": "l1",
                "final_status": "closed_negative_pnl",
                "overall_pnl": -2.0,
                "start_index": 20,
                "input_slice_start_index": 0,
                "start_time": "2025-12-01T01:40:00+00:00",
            },
            {
                "trade_block_id": "o1",
                "final_status": "open",
                "overall_pnl": 0.5,
                "start_index": 30,
                "input_slice_start_index": 0,
                "start_time": "2025-12-01T02:30:00+00:00",
            },
            {
                "trade_block_id": "u1",
                "final_status": "closed_undercovered_final_exit",
                "overall_pnl": 0.8,
                "start_index": 40,
                "input_slice_start_index": 0,
                "start_time": "2025-12-01T03:20:00+00:00",
            },
        ],
    }
    path = tmp_path / "res.json"
    path.write_text(json.dumps(result), encoding="utf-8")
    candles = _synth_candles(80)
    extracted = extract_trades_from_result_file(
        path, candles=candles, trade_filter="positive_closed"
    )
    ids = {t["trade_id"] for t in extracted["trades"]}
    assert ids == {"w1", "u1"}
    assert extracted["trade_count"] == 2
    w1 = next(t for t in extracted["trades"] if t["trade_id"] == "w1")
    assert w1["start_index"] == 10
    assert w1["decision_time_after_close_utc"] == (
        pd.Timestamp(w1["start_candle_open_utc"]) + pd.Timedelta(minutes=5)
    ).isoformat()


def test_start_index_absolute_resolution() -> None:
    from research.regime_scanner.trade_list_builder import absolute_start_index

    assert absolute_start_index({"start_index": 483, "input_slice_start_index": 2569}) == 3052


def test_strict_groups_and_net_effect_signs() -> None:
    from research.regime_scanner.batch_audit import (
        compare_filter_rules,
        evaluate_strict_long_rule,
    )

    winners = [
        {"trade_id": "w1", "status": "success", "combined_regime": "bullish_trend", "pnl": 2.0},
        {"trade_id": "w2", "status": "success", "combined_regime": "transition", "pnl": 3.0},
        {"trade_id": "w3", "status": "success", "combined_regime": "bullish_trend_with_trend_weakness", "pnl": 1.0},
    ]
    losers = [
        {"trade_id": "l1", "status": "success", "combined_regime": "transition", "pnl": -4.0},
        {"trade_id": "l2", "status": "success", "combined_regime": "bullish_trend", "pnl": -1.0},
    ]
    strict = evaluate_strict_long_rule(winners)
    assert strict["allowed_count"] == 1
    assert strict["blocked_count"] == 2
    assert strict["allowed_pnl_sum"] == pytest.approx(2.0)
    assert strict["blocked_pnl_sum"] == pytest.approx(4.0)

    comparison = compare_filter_rules(
        winner_rows=winners,
        loser_rows=losers,
        original_total_pnl=1.0,  # 2+3+1-4-1
    )
    # Rule F: block everything except bullish_trend/strong
    f = next(r for r in comparison["rules"] if r["label"] == "F")
    assert f["blocked_winners"] == 2
    assert f["blocked_losers"] == 1  # only transition loser
    assert f["forgone_gain"] == pytest.approx(4.0)
    assert f["avoided_loss"] == pytest.approx(4.0)
    assert f["net_effect"] == pytest.approx(0.0)
    assert f["hypothetical_total_pnl"] == pytest.approx(1.0)
    assert f["precision_blocked_are_losers"] == pytest.approx(1 / 3)
    assert f["recall_losers_blocked"] == pytest.approx(0.5)

    # Rule B: block only transition
    b = next(r for r in comparison["rules"] if r["label"] == "B")
    assert b["blocked_winners"] == 1
    assert b["blocked_losers"] == 1
    assert b["forgone_gain"] == pytest.approx(3.0)
    assert b["avoided_loss"] == pytest.approx(4.0)
    assert b["net_effect"] == pytest.approx(1.0)
    assert b["hypothetical_total_pnl"] == pytest.approx(2.0)


def test_profitable_batch_runs_on_synth(tmp_path: Path) -> None:
    from research.regime_scanner.batch_audit import (
        run_batch_audit_from_trades,
        write_batch_outputs,
    )
    from research.regime_scanner.trade_list_builder import extract_trades_from_result_file

    candles = _synth_candles(400)
    result = {
        "metadata": {},
        "aggregate": [{"total_pnl": 0.5, "successful_closed_count": 1}],
        "runs": [
            {
                "trade_block_id": "w1",
                "final_status": "closed",
                "overall_pnl": 0.5,
                "start_index": 200,
                "input_slice_start_index": 0,
                "start_time": str(candles["timestamp"].iloc[200]),
            }
        ],
    }
    path = tmp_path / "res.json"
    path.write_text(json.dumps(result), encoding="utf-8")
    extracted = extract_trades_from_result_file(
        path, candles=candles, trade_filter="positive_closed"
    )
    payload = run_batch_audit_from_trades(
        extracted["trades"],
        symbol="SYN",
        timeframes="5m,15m,30m",
        candles=candles,
    )
    assert payload["summary"]["trade_count"] == 1
    assert payload["summary"]["successes"] == 1
    paths = write_batch_outputs(payload, tmp_path / "out", prefix="profitable")
    assert paths["csv"].name == "profitable_regime_audit_rows.csv"
    safe = json_safe(payload)
    json.dumps(safe, allow_nan=False)


@pytest.mark.skipif(
    not (DEFAULT_DATA_DIR / symbol_to_feather_name("APTUSDT")).exists(),
    reason="external APT feather file not present",
)
def test_real_positive_extraction_count() -> None:
    from research.regime_scanner.data_loader import load_symbol_candles
    from research.regime_scanner.trade_list_builder import extract_trades_from_result_file

    result_file = Path(
        "research/backtests/results/full_history_continuous_long_recovery/"
        "APTUSDT_original_hedge_5m_continuous_results.json"
    )
    if not result_file.exists():
        pytest.skip("result file missing")
    candles = load_symbol_candles("APTUSDT")
    extracted = extract_trades_from_result_file(
        result_file, candles=candles, trade_filter="positive_closed"
    )
    assert extracted["trade_count"] == 79
    assert all(t["pnl"] > 0 for t in extracted["trades"])
    assert all(t["category"] == "positive_closed" for t in extracted["trades"])

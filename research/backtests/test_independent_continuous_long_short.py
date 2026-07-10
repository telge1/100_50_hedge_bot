from __future__ import annotations

import json
from pathlib import Path

import pytest

from research.backtests.independent_continuous_long_short_analysis import (
    combine_independent_summaries,
    merge_timeline,
    summarize_direction_runs,
    validate_independent_reentry,
)
from research.backtests.paired_direction_recovery import mirror_recovery_start_purpose


def test_short_recovery_purpose_mirror() -> None:
    assert mirror_recovery_start_purpose("CYCLE_4_LONG_ADD") == "CYCLE_4_SHORT_REDUCE"


def test_independent_reentry_offsets() -> None:
    long_runs = [
        {"trade_number": 1, "start_index": 0, "end_index": 10},
        {"trade_number": 2, "start_index": 11, "end_index": 20},
    ]
    short_runs = [
        {"trade_number": 1, "start_index": 0, "end_index": 5},
        {"trade_number": 2, "start_index": 6, "end_index": 15},
        {"trade_number": 3, "start_index": 16, "end_index": 25},
    ]
    validation = validate_independent_reentry(long_runs, short_runs)
    assert validation["long"]["reentry_offset_ok"] is True
    assert validation["short"]["reentry_offset_ok"] is True
    assert validation["trade_count_differs"] is True


def test_combined_pnl_is_additive() -> None:
    long_summary = summarize_direction_runs(
        [{"realized_pnl": 2.0, "mark_to_market_pnl": 2.0, "candles_processed": 5, "final_status": "closed"}],
        direction="long",
    )
    short_summary = summarize_direction_runs(
        [{"realized_pnl": -0.5, "mark_to_market_pnl": -0.5, "candles_processed": 3, "final_status": "closed"}],
        direction="short",
    )
    combined = combine_independent_summaries(long_summary, short_summary)
    assert combined["combined_realized_pnl"] == pytest.approx(1.5)
    assert combined["combined_mtm_pnl"] == pytest.approx(1.5)


def test_timeline_is_chronological_not_paired() -> None:
    long_runs = [{"trade_number": 1, "start_index": 0, "end_index": 10, "start_time": "t0", "end_time": "t1", "candles_processed": 10, "final_status": "closed", "realized_pnl": 1.0}]
    short_runs = [
        {"trade_number": 1, "start_index": 0, "end_index": 5, "start_time": "t0", "end_time": "t2", "candles_processed": 5, "final_status": "closed", "realized_pnl": 0.5},
        {"trade_number": 2, "start_index": 6, "end_index": 12, "start_time": "t3", "end_time": "t4", "candles_processed": 6, "final_status": "closed", "realized_pnl": 0.2},
    ]
    rows = merge_timeline(long_runs, short_runs)
    assert len(rows) == 3
    assert rows[0]["bot_direction"] == "long"
    assert rows[1]["bot_direction"] == "short"
    assert rows[2]["bot_direction"] == "short"


def test_long_reference_reproduces_expected_metrics() -> None:
    path = Path(
        "research/backtests/results/integrated_recovery_parameter_sweep_20260709T150115Z/"
        "variants/CYCLE_4_LONG_ADD_wait_576/APTUSDT_original_hedge_5m_continuous_results.json"
    )
    if not path.is_file():
        pytest.skip("long reference missing")
    runs = json.loads(path.read_text(encoding="utf-8"))["runs"]
    summary = summarize_direction_runs(runs, direction="long")
    assert summary["trades_started"] == 226
    assert summary["net_realized_pnl"] == pytest.approx(29.596409332329458)
    assert summary["total_mark_to_market_pnl"] == pytest.approx(29.372757759961257, rel=1e-4)
    assert summary["recovery_closed_count"] == 17


def test_shared_initial_start_index_zero() -> None:
    path = Path(
        "research/backtests/results/integrated_recovery_parameter_sweep_20260709T150115Z/"
        "variants/CYCLE_4_LONG_ADD_wait_576/APTUSDT_original_hedge_5m_continuous_results.json"
    )
    if not path.is_file():
        pytest.skip("long reference missing")
    first = json.loads(path.read_text(encoding="utf-8"))["runs"][0]
    assert first["start_index"] == 0

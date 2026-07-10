from __future__ import annotations

import json
from pathlib import Path

import pytest

from research.backtests.paired_direction_recovery import mirror_recovery_start_purpose
from research.backtests.paired_long_short_analysis import (
    build_pair_comparison_rows,
    classify_recovery_offset,
    combine_summaries,
    summarize_direction_runs,
)
from research.backtests.paired_start_schedule import (
    build_paired_start_schedule,
    trade_mark_to_market,
)


def test_mirror_recovery_purpose_c4_long_add_to_short_reduce() -> None:
    assert mirror_recovery_start_purpose("CYCLE_4_LONG_ADD") == "CYCLE_4_SHORT_REDUCE"


def test_build_schedule_from_long_results() -> None:
    path = Path(
        "research/backtests/results/integrated_recovery_parameter_sweep_20260709T150115Z/"
        "variants/CYCLE_4_LONG_ADD_wait_576/APTUSDT_original_hedge_5m_continuous_results.json"
    )
    if not path.is_file():
        pytest.skip("long reference results missing")
    schedule = build_paired_start_schedule(
        path,
        long_recovery_purpose="CYCLE_4_LONG_ADD",
        recovery_wait_candles=576,
    )
    assert schedule["pair_count"] == 226
    first = schedule["pairs"][0]
    assert first["start_index"] == 0
    assert first["start_time"] == "2025-12-27T00:00:00+00:00"
    assert first["reference_entry_price"] == pytest.approx(1.676)


def test_pair_rows_share_start_index() -> None:
    long_runs = [
        {
            "pair_number": 1,
            "trade_number": 1,
            "start_index": 100,
            "start_time": "2026-01-01T00:00:00+00:00",
            "reference_entry_price": 2.0,
            "end_index": 120,
            "end_time": "2026-01-01T01:40:00+00:00",
            "candles_processed": 20,
            "final_status": "closed",
            "realized_pnl": 0.5,
            "recovery_activated": False,
        }
    ]
    short_runs = {
        1: {
            "pair_number": 1,
            "trade_number": 1,
            "start_index": 100,
            "start_time": "2026-01-01T00:00:00+00:00",
            "entry_price": 2.0,
            "end_index": 130,
            "end_time": "2026-01-01T02:30:00+00:00",
            "candles_processed": 30,
            "final_status": "closed",
            "realized_pnl": -0.1,
            "recovery_activated": False,
        }
    }
    rows = build_pair_comparison_rows(
        long_runs=long_runs,
        short_runs_by_pair=short_runs,
        short_mtm_at_long_exit_by_pair={1: 0.2},
        long_mtm_at_short_exit_by_pair={},
    )
    assert rows[0]["shared_start_index"] == 100
    assert rows[0]["shared_start_timestamp"] == "2026-01-01T00:00:00+00:00"
    assert rows[0]["short_mtm_at_long_exit"] == pytest.approx(0.2)
    assert rows[0]["combined_mtm_at_long_exit"] == pytest.approx(0.7)


def test_classify_recovery_offset() -> None:
    assert classify_recovery_offset(-1.0, 0.2) == "fully_offset"
    assert classify_recovery_offset(-1.0, -0.4) == "partially_offset"
    assert classify_recovery_offset(-1.0, -1.5) == "not_offset"


def test_combine_summaries_adds_pnl() -> None:
    long_summary = summarize_direction_runs(
        [{"realized_pnl": 1.0, "mark_to_market_pnl": 1.0, "candles_processed": 10, "final_status": "closed"}],
        direction="long",
    )
    short_summary = summarize_direction_runs(
        [{"realized_pnl": -0.2, "mark_to_market_pnl": -0.2, "candles_processed": 8, "final_status": "closed"}],
        direction="short",
    )
    combined = combine_summaries(long_summary, short_summary)
    assert combined["combined_realized_pnl"] == pytest.approx(0.8)
    assert combined["combined_mark_to_market_pnl"] == pytest.approx(0.8)


def test_trade_mark_to_market_open_position() -> None:
    mtm = trade_mark_to_market(
        {
            "realized_pnl": -0.2,
            "final_long_qty": 10.0,
            "final_short_qty": 5.0,
            "final_long_avg_price": 2.0,
            "final_short_avg_price": 2.0,
            "entry_price": 2.0,
        }
    )
    assert mtm["mark_to_market_pnl"] < mtm["realized_pnl"]


def test_fail_closed_missing_schedule_source(tmp_path: Path) -> None:
    bad = tmp_path / "missing.json"
    with pytest.raises(FileNotFoundError):
        build_paired_start_schedule(bad, long_recovery_purpose="CYCLE_4_LONG_ADD", recovery_wait_candles=576)

"""Comparator tests only. Does not load ClickHouse or start a warmup."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bot.e1r_live_scanner.resume_parity_proof import (
    LIVE_CHECKPOINT_DIR,
    compare_end_states,
    run_one_symbol_resume_parity,
)


def _state() -> dict:
    return {
        "last_processed_15m_close": "2026-06-07T09:00:00+00:00",
        "seen_no_guard": ["a"],
        "seen_with_guard": ["b"],
        "e1r_state": "ACTIVE",
        "e1r_machine": {"2026-06-07T09:00:00+00:00": "ACTIVE"},
        "pending": None,
        "e1r_ep": {"start_time": "2026-06-07T08:45:00+00:00", "cluster_2_pool_ids": ["c2"]},
        "reclaim_events": [{"result": "EMA_RECLAIM_SUCCESS"}],
        "e1r_snapshot_hash": "abc",
        "floor": {"block_until": None, "session_floor_top": None, "session_floor_bottom": None, "last_reason": None},
        "frozen_pool_ids": ["p1"],
        "frozen_rows_hash": "fff",
        "e1r_lld_pool_ids": ["e1"],
        "e1r_lld_rows_hash": "eee",
        "signals": [{"pool_id": "p1", "signal_status": "ALLOWED"}],
    }


def test_equal_states_pass():
    result = compare_end_states(_state(), _state(), bar="2026-06-07T09:00:00+00:00")
    assert result["pass"] is True
    assert set(result["verdicts"].values()) == {"PASS"}


def test_seen_mismatch_is_exact_fail():
    other = _state()
    other["seen_no_guard"] = ["a", "z"]
    result = compare_end_states(_state(), other, bar="2026-06-07T09:15:00+00:00")
    assert result["pass"] is False
    assert result["verdicts"]["seen_no_guard"] == "FAIL"
    assert result["diffs"][0]["bar"] == "2026-06-07T09:15:00+00:00"
    assert result["diffs"][0]["resume_only"] == ["z"]


def test_machine_diff_reports_first_key_only():
    other = _state()
    other["e1r_machine"] = {"2026-06-07T09:00:00+00:00": "PENDING"}
    result = compare_end_states(_state(), other)
    diff = next(item for item in result["diffs"] if item["field"] == "e1r_machine")
    assert diff["first_key"] == "2026-06-07T09:00:00+00:00"
    assert diff["full"] == "ACTIVE"
    assert diff["resume"] == "PENDING"


def test_proof_does_not_start_without_an_explicit_horizon(tmp_path):
    with pytest.raises(RuntimeError, match="full-tip warmup"):
        run_one_symbol_resume_parity(checkpoint_dir=tmp_path)


def test_proof_refuses_live_checkpoint_directory():
    with pytest.raises(ValueError, match="live checkpoint"):
        run_one_symbol_resume_parity(
            through_close=datetime(2026, 6, 7, tzinfo=timezone.utc),
            checkpoint_dir=LIVE_CHECKPOINT_DIR,
        )

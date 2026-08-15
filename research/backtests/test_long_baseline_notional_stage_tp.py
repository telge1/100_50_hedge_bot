"""Tests for L0/L1 long baseline notional stage-TP audit helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from research.backtests.backtest_report import BacktestResult
from research.backtests.long_baseline_notional_stage_tp import (
    L0_LONG_NOTIONAL,
    L1_LONG_NOTIONAL,
    build_baseline_call_kwargs,
    check_l0_parity,
    extract_stage_tp_attempts,
    freeze_guard_inactive,
    start_parity_row,
    summarize_variant,
)
from research.backtests.run_current_baseline_multicoin_blocker_audit import (
    LONG_FILL_DISTANCE_PCT,
    TARGET_PROFIT_USDT,
    TP_PROFIT_TARGET_PCT,
)


def test_build_baseline_call_kwargs_no_policy_overrides() -> None:
    kwargs = build_baseline_call_kwargs(symbol="BTCUSDT", candles=[], base_notional_usdt=1000.0)
    assert kwargs["base_notional_usdt"] == 1000.0
    assert kwargs["initial_notional_usdt"] == 1000.0
    assert "exit_rebuild_policy_config" not in kwargs
    assert "inventory_mtm_freeze_config" not in kwargs
    assert "recovery_reentry_config" not in kwargs
    assert kwargs["long_fill_distance_pct"] == LONG_FILL_DISTANCE_PCT
    assert kwargs["target_profit_usdt"] == TARGET_PROFIT_USDT
    assert kwargs["tp_profit_target_pct"] == TP_PROFIT_TARGET_PCT


def test_extract_stage_tp_attempts_fallback_and_accept() -> None:
    result = BacktestResult(symbol="TESTUSDT", direction="long")
    result.intent_log = [
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "purpose": "CYCLE_1_SHORT_REDUCE",
            "side": "short",
            "qty": 1.0,
            "trigger_price": 100.0,
            "metadata_excerpt": {
                "fallback_to_single_second_leg": True,
                "split_fallback_reason": "stage_below_min_notional",
                "rejected_stage_count": 3,
                "rejected_stage_notional_values": [4.0, 4.0, 4.0],
                "original_stage_count": 3,
            },
        },
        {
            "timestamp": "2026-01-02T00:00:00+00:00",
            "purpose": "CYCLE_2_SHORT_REDUCE",
            "side": "short",
            "qty": 0.5,
            "trigger_price": 200.0,
            "metadata_excerpt": {
                "is_staged_second_leg_tp": True,
                "stage_index": 0,
                "stage_count": 2,
            },
        },
        {
            "timestamp": "2026-01-02T00:00:00+00:00",
            "purpose": "CYCLE_2_SHORT_REDUCE",
            "side": "short",
            "qty": 0.5,
            "trigger_price": 210.0,
            "metadata_excerpt": {
                "is_staged_second_leg_tp": True,
                "stage_index": 1,
                "stage_count": 2,
            },
        },
    ]
    rows = extract_stage_tp_attempts(
        coin="TESTUSDT",
        variant="L0",
        trade_number=1,
        result=result,
        exchange_min_notional=5.0,
    )
    assert len(rows) == 2
    rejected = next(r for r in rows if r["cycle"] == 1)
    accepted = next(r for r in rows if r["cycle"] == 2)
    assert rejected["rejected"] == 1
    assert rejected["full_second_leg_fallback_used"] == 1
    assert accepted["accepted"] == 1
    assert accepted["actual_stage_count"] == 2


def test_start_parity_row_index_zero() -> None:
    row = start_parity_row(
        coin="X",
        candles=[],
        l0_first={"start_index": 0, "start_timestamp": "t0"},
        l1_first={"start_index": 0, "start_timestamp": "t0"},
    )
    assert row["first_entry_index_L0"] == 0
    assert row["first_entry_index_L1"] == 0
    assert row["start_parity_pass"] == 1


def test_freeze_guard_inactive_on_empty_excerpt() -> None:
    result = BacktestResult(symbol="X", direction="long")
    result.final_strategy_state_excerpt = {}
    assert freeze_guard_inactive(result) is True


def test_l0_parity_targets_structure() -> None:
    summary = {
        "trades_started": 265,
        "trades_closed": 238,
        "open_blocker_count": 27,
        "closed_pnl_usdt": 60.70230719517889,
        "total_series_mtm_usdt": -291.96557591506945,
        "invalid_partial_cycle_count": 0,
    }
    parity = check_l0_parity(summary)
    assert parity["ok"] is True


def test_summarize_variant_counts() -> None:
    rows = [
        {"is_blocker": 0, "closed_pnl_usdt": 1.0, "mtm_pnl": 1.0, "duration_candles": 10, "max_cycle": 1},
        {"is_blocker": 1, "closed_pnl_usdt": 0.0, "mtm_pnl": -5.0, "duration_candles": 100, "max_cycle": 4},
    ]
    s = summarize_variant(rows, variant="L0", long_notional=L0_LONG_NOTIONAL, short_notional=50.0)
    assert s["trades_started"] == 2
    assert s["trades_closed"] == 1
    assert s["open_blocker_count"] == 1


def test_notional_constants() -> None:
    assert L1_LONG_NOTIONAL / L0_LONG_NOTIONAL == 10.0


def test_protected_output_not_default_tmp(tmp_path: Path) -> None:
    from research.backtests.run_long_baseline_1000_500_stage_tp_audit import PROTECTED

    assert any("current_baseline" in str(p) for p in PROTECTED)

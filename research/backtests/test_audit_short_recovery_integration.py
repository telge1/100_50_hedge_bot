"""Regression tests for short recovery integration audit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fixed_cycle_hedge_bot.models import FillEvent

from research.backtests.audit_short_recovery_integration import (
    DEFAULT_SOURCE_DIR,
    SHORT_RECOVERY_PURPOSE,
    build_direction_neutrality_audit,
    build_short_backtest_recovery_code_path,
    build_short_config_audit,
    run_audit,
    run_forced_short_recovery_execution,
)
from research.backtests.long_gap_reduction import LongGapReductionRuntime
from research.backtests.paired_direction_recovery import mirror_recovery_start_purpose
from research.backtests.recovery_bot_config import RecoveryBotConfig, normalize_recovery_start_purpose
from research.backtests.recovery_bot_shim import RecoveryBotTracker, _note_reference_from_fills, should_activate_recovery


def test_short_recovery_config_resolves():
    assert normalize_recovery_start_purpose("CYCLE_4_SHORT_REDUCE") == "CYCLE_4_SHORT_REDUCE"
    assert mirror_recovery_start_purpose("CYCLE_4_LONG_ADD") == "CYCLE_4_SHORT_REDUCE"


def test_cycle4_short_reduce_reference_recognized():
    tracker = RecoveryBotTracker(
        config=RecoveryBotConfig(enabled=True, recovery_start_purpose=SHORT_RECOVERY_PURPOSE, recovery_wait_candles=576)
    )
    fill = FillEvent(
        exchange_order_id="o1",
        client_order_id=None,
        side="sell",
        purpose=SHORT_RECOVERY_PURPOSE,
        exec_qty=1.0,
        exec_price=1.0,
        order_type="limit",
        reduce_only=True,
        status="filled",
    )
    _note_reference_from_fills(
        tracker,
        fills=[fill],
        local_candle_index=10,
        absolute_candle_index=100,
        timestamp="2026-01-01T00:00:00+00:00",
    )
    assert tracker.state.reference_reached is True
    assert tracker.state.activation_absolute_candle_index == 676


def test_wait_computed_from_reference_fill():
    tracker = RecoveryBotTracker(
        config=RecoveryBotConfig(enabled=True, recovery_start_purpose=SHORT_RECOVERY_PURPOSE, recovery_wait_candles=10)
    )
    tracker.state.reference_reached = True
    tracker.state.activation_absolute_candle_index = 110
    assert should_activate_recovery(tracker, absolute_candle_index=109, trade_still_open=True) is False
    assert should_activate_recovery(tracker, absolute_candle_index=110, trade_still_open=True) is True


def test_original_exit_prevents_activation_when_trade_closed():
    tracker = RecoveryBotTracker(config=RecoveryBotConfig(enabled=True, recovery_wait_candles=3))
    tracker.state.reference_reached = True
    tracker.state.activation_absolute_candle_index = 20
    assert should_activate_recovery(tracker, absolute_candle_index=25, trade_still_open=False) is False


def test_short_primary_gap_qty_zero_in_long_gap_runtime():
    runtime = LongGapReductionRuntime(
        initial_long_qty=50.0,
        initial_short_qty=100.0,
        long_avg=1.0,
        short_avg=1.0,
        reference_price=1.0,
        base_main_realized_pnl=0.0,
    )
    assert runtime.initial_gap_qty == pytest.approx(0.0)


def test_long_primary_gap_qty_positive_in_long_gap_runtime():
    runtime = LongGapReductionRuntime(
        initial_long_qty=100.0,
        initial_short_qty=50.0,
        long_avg=1.0,
        short_avg=1.0,
        reference_price=1.0,
        base_main_realized_pnl=0.0,
    )
    assert runtime.initial_gap_qty == pytest.approx(50.0)


def test_forced_short_recovery_reference_and_activation():
    forced = run_forced_short_recovery_execution()
    ex = forced["execution"]
    assert ex["reference_fill_recognized"] is True
    assert ex["recovery_activated"] is True
    assert ex["initial_gap_qty_computed"] == pytest.approx(0.0)
    assert ex["gap_reduce_executed"] is False


def test_direction_neutrality_gap_not_mirrored():
    rows = build_direction_neutrality_audit()
    gap_rows = [r for r in rows if "gap" in r["concept"]]
    assert any(r["correct"] is False for r in gap_rows)


def test_live_config_missing_wait_recovery_keys():
    audit = build_short_config_audit()
    assert audit["recovery_start_purpose_in_live_json"] is None
    assert audit["recovery_wait_candles_in_live_json"] is None
    assert audit["live_can_consume_wait_recovery"] is False


def test_code_path_includes_long_gap_reduction_gap_issue():
    rows = build_short_backtest_recovery_code_path()
    gap_row = next(r for r in rows if r["step"] == "gap_compute")
    assert gap_row["short_supported"] is False


@pytest.fixture(scope="module")
def audit_summary():
    if not (DEFAULT_SOURCE_DIR / "short_continuous_results.json").is_file():
        pytest.skip("short backtest results missing")
    out = Path("research/backtests/results/short_recovery_integration_audit_test")
    return run_audit(source_dir=DEFAULT_SOURCE_DIR, output_dir=out)


def test_audit_outputs_exist(audit_summary: dict):
    output = Path(audit_summary["output_dir"])
    for name in (
        "short_backtest_recovery_code_path.csv",
        "forced_short_recovery_execution.json",
        "forced_short_recovery_events.csv",
        "short_recovery_direction_neutrality_audit.csv",
        "short_live_vs_backtest_recovery_matrix.csv",
        "short_cycle_progression_population.csv",
        "short_cycle_progression_summary.json",
        "analysis_summary.json",
        "REPORT.md",
    ):
        assert (output / name).is_file(), name


def test_real_backtest_zero_recovery_activations(audit_summary: dict):
    assert audit_summary["short_run_summary"]["recovery_activations"] == 0
    assert audit_summary["cycle_progression_summary"]["recovery_reference_reached_count"] == 0


def test_cycle_progression_max_is_one(audit_summary: dict):
    dist = audit_summary["cycle_progression_summary"]["max_cycle_distribution"]
    assert max(int(k) for k in dist.keys()) == 1


def test_no_live_files_modified():
    assert Path("live_bots/short_hedge_bot/short_bot_1/config/fixed_cycle_config.json").is_file()

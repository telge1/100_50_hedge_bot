"""Regression tests for short cycle-1 second leg audit."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from fixed_cycle_hedge_bot.cycle_sequence import STEP_WAITING_FOR_PAIR_SECOND_LEG
from fixed_cycle_hedge_bot.fixed_cycle_strategy import (
    FixedCycleHedgeConfig,
    ShortFixedCycleHedgeStrategy,
)
from fixed_cycle_hedge_bot.models import HedgeSnapshot, RuntimeState

from research.backtests.audit_short_cycle1_second_leg import (
    DEFAULT_OUTPUT_DIR,
    DEFAULT_SOURCE_DIR,
    _compute_loss_cover_long_reduce_trigger,
    _short_sequence,
    analyze_trade_population,
    build_expected_cycle1_sequence_doc,
    build_trade_0061_timeline,
    run_audit,
)


@pytest.fixture(scope="module")
def source_dir() -> Path:
    if not DEFAULT_SOURCE_DIR.is_dir():
        pytest.skip(f"missing backtest results: {DEFAULT_SOURCE_DIR}")
    return DEFAULT_SOURCE_DIR


def test_expected_short_first_second_leg_purposes():
    seq = _short_sequence()
    assert seq.first_leg_purpose == "CYCLE_1_SHORT_REDUCE"
    assert seq.second_leg_purpose == "CYCLE_1_LONG_REDUCE"


def test_sequence_doc_has_second_leg_waiting_field():
    doc = build_expected_cycle1_sequence_doc()
    assert doc["short_primary"]["waiting_flag_field"] == "cycle_waiting_for_long_reduce"
    assert doc["short_primary"]["next_required_purpose_after_first_leg"] == "CYCLE_1_LONG_REDUCE"


def test_second_leg_blocked_without_confirmed_pnl_on_cycle_entry():
    strategy = ShortFixedCycleHedgeStrategy(
        FixedCycleHedgeConfig(symbol="APTUSDT", target_profit_usdt=0.25, restart=False)
    )
    state = {
        "cycle_waiting_for_long_reduce": True,
        "long_reduce_pending_cycle": 1,
        "active_cycle_index": 1,
        "cycle_step": STEP_WAITING_FOR_PAIR_SECOND_LEG,
        "next_required_purpose": "CYCLE_1_LONG_REDUCE",
        "processed_cycle_purposes": ["CYCLE_1_SHORT_REDUCE"],
        "initial_long_qty": 62.8,
        "cycle_states": {
            "1": {
                "short_reduce_status": "PROCESSED",
                "long_reduce_status": "NONE",
                "short_reduce_fill_price": 0.7995,
                "short_reduce_fill_confirmed": True,
            }
        },
    }
    runtime = RuntimeState(
        strategy_state=state,
        last_snapshot=HedgeSnapshot(
            symbol="APTUSDT",
            current_price=0.79,
            long_qty=62.8,
            short_qty=94.2,
            long_avg=0.7955,
            short_avg=0.7955,
        ),
    )
    captured: list[dict] = []
    ctx = mock.Mock()
    ctx.audit.log_event = lambda event, **payload: captured.append({"event": event, **payload})

    intents = strategy._build_short_tp_follow_up(runtime.last_snapshot, runtime, ctx)
    assert intents == []
    skip_reasons = [c.get("reason") for c in captured if c.get("event") == "fixed_cycle_short_tp_follow_up_skip"]
    assert "long_reduce_blocked_until_confirmed_pnl" in skip_reasons


def test_loss_cover_trigger_direction_up():
    trigger = _compute_loss_cover_long_reduce_trigger(
        long_avg=0.7955,
        long_reduce_qty=15.7,
        loss_usdt=0.1257,
        target_profit_usdt=0.25,
    )
    assert trigger is not None
    assert trigger > 0.7955


def test_population_all_order_not_created(source_dir: Path):
    population, root = analyze_trade_population(source_dir, [])
    assert len(population) == 50
    assert all(not row["second_leg_created"] for row in population)
    assert root[0]["classification"] == "order_not_created"


def test_trade_61_reproduced(source_dir: Path):
    candles = []
    timeline, analysis = build_trade_0061_timeline(source_dir, candles)
    assert analysis["duration_candles"] == 29091
    assert analysis["first_leg_fill"]["fill_price"] == pytest.approx(0.7995, rel=1e-3)
    assert analysis["second_leg_CYCLE_1_LONG_REDUCE"]["created_in_trade_blocks"] is False
    assert any(row.get("notes") == "first_leg_fill" for row in timeline)


def test_run_audit_outputs(source_dir: Path, tmp_path: Path):
    out = tmp_path / "audit_out"
    summary = run_audit(source_dir=source_dir, output_dir=out)
    assert summary["second_leg_created_count"] == 0
    for name in (
        "trade_0061_state_timeline.csv",
        "trade_0061_analysis.json",
        "short_cycle1_second_leg_population.csv",
        "short_cycle1_second_leg_root_causes.csv",
        "cycle1_trigger_formula_comparison.csv",
        "long_short_cycle1_transition_comparison.csv",
        "analysis_summary.json",
        "REPORT.md",
    ):
        assert (out / name).is_file(), name
    payload = json.loads((out / "analysis_summary.json").read_text())
    assert payload["population_trades"] == 50


def test_baseline_unchanged_marker(source_dir: Path):
    """Audit is read-only — source result trade count stays 89."""
    runs = json.loads((source_dir / "short_continuous_results.json").read_text())["runs"]
    assert len(runs) == 89

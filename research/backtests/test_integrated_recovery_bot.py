from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from research.backtests.backtest_report import BacktestResult
from research.backtests.continuous_reentry_backtest import (
    CONTINUOUS_SUCCESSFUL_EXIT_REASONS,
    run_continuous_reentry_for_direction,
)
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.long_gap_reduction import (
    LongGapReductionConfig,
    LongGapReductionRuntime,
    planned_reduce_qty_per_step,
)
from research.backtests.recovery_bot_config import RecoveryBotConfig, default_recovery_bot_config
from research.backtests.recovery_bot_shim import (
    should_activate_recovery,
    trade_absolute_candle_index,
)
from research.backtests.simulated_order_book import SyntheticCandle


def _candle(ts: datetime, close: float, low: float | None = None) -> SyntheticCandle:
    return SyntheticCandle(
        symbol="TESTUSDT",
        timestamp=ts,
        open=close,
        high=close,
        low=low if low is not None else close,
        close=close,
    )


def test_recovery_bot_default_disabled() -> None:
    cfg = default_recovery_bot_config()
    assert cfg.enabled is False


def test_planned_reduce_qty_uses_fraction() -> None:
    cfg = LongGapReductionConfig(num_steps=4, gap_reduce_fraction_per_step=0.25)
    assert planned_reduce_qty_per_step(initial_gap_qty=8.0, cfg=cfg) == pytest.approx(2.0)


def test_activation_index_math() -> None:
    absolute = trade_absolute_candle_index(
        input_slice_start_index=0,
        absolute_trade_start_index=10,
        local_candle_index=5,
    )
    assert absolute == 15


def test_should_not_activate_before_wait_end() -> None:
    from research.backtests.recovery_bot_shim import RecoveryBotTracker, RecoveryBotState

    tracker = RecoveryBotTracker(config=RecoveryBotConfig(enabled=True, recovery_wait_candles=3))
    tracker.state.reference_reached = True
    tracker.state.activation_absolute_candle_index = 20
    assert should_activate_recovery(tracker, absolute_candle_index=19, trade_still_open=True) is False
    assert should_activate_recovery(tracker, absolute_candle_index=20, trade_still_open=True) is True


def test_runtime_four_steps_close_gap() -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = []
    prices = [100.0, 99.0, 98.01, 97.0299, 96.059601, 95.0]
    for index, price in enumerate(prices):
        candles.append(_candle(base + timedelta(minutes=5 * index), price, low=price))

    runtime = LongGapReductionRuntime(
        initial_long_qty=10.0,
        initial_short_qty=6.0,
        long_avg=100.0,
        short_avg=100.0,
        reference_price=100.0,
        base_main_realized_pnl=-1.0,
        cfg=LongGapReductionConfig(
            num_steps=4,
            gap_reduce_fraction_per_step=0.25,
            fee_rate=None,
        ),
    )
    runtime.start_event(candles[0], local_candle_index=0, absolute_candle_index=0)
    completed = False
    for index, candle in enumerate(candles[1:], start=1):
        step = runtime.process_candle(candle, local_candle_index=index, absolute_candle_index=index)
        if step.recovery_completed:
            completed = True
            break
    summary = runtime.summary()
    assert completed is True
    assert summary["gap_fully_closed"] is True
    assert summary["initial_gap_qty"] == pytest.approx(4.0)
    assert summary["planned_gap_reduce_qty_per_step"] == pytest.approx(1.0)
    assert summary["total_reduced_qty"] == pytest.approx(4.0)


def test_recovery_off_unchanged_exit_reason_set() -> None:
    assert "flat_no_active_orders" in CONTINUOUS_SUCCESSFUL_EXIT_REASONS
    assert "recovery_joint_exit" in CONTINUOUS_SUCCESSFUL_EXIT_REASONS


def test_continuous_reentry_accepts_recovery_joint_exit_for_next_trade(tmp_path: Path) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = [
        {
            "timestamp": (base + timedelta(minutes=5 * i)).isoformat(),
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
        }
        for i in range(3)
    ]
    results = run_continuous_reentry_for_direction(
        "TESTUSDT",
        "long",
        candles,
        continuous_max_trades=5,
        recovery_bot_config=None,
    )
    assert isinstance(results, list)


def test_recovery_bot_config_from_cli_defaults() -> None:
    cfg = default_recovery_bot_config()
    cfg.enabled = True
    cfg.recovery_start_purpose = "CYCLE_4_LONG_ADD"
    cfg.recovery_wait_candles = 144
    assert cfg.recovery_gap_reduce_steps == 4
    assert cfg.recovery_gap_reduce_fraction_per_step == 0.25


def test_reference_index_set_on_c4_long_add_fill() -> None:
    from fixed_cycle_hedge_bot.models import FillEvent

    from research.backtests.recovery_bot_shim import RecoveryBotTracker, _note_reference_from_fills

    tracker = RecoveryBotTracker(
        config=RecoveryBotConfig(enabled=True, recovery_start_purpose="CYCLE_4_LONG_ADD", recovery_wait_candles=144)
    )
    fill = FillEvent(
        exchange_order_id="o1",
        client_order_id=None,
        side="buy",
        purpose="CYCLE_4_LONG_ADD",
        exec_qty=1.0,
        exec_price=10.0,
        order_type="limit",
        reduce_only=False,
        status="filled",
    )
    _note_reference_from_fills(
        tracker,
        fills=[fill],
        local_candle_index=50,
        absolute_candle_index=150,
        timestamp="2026-01-10T12:00:00+00:00",
    )
    assert tracker.state.reference_reached is True
    assert tracker.state.reference_absolute_candle_index == 150
    assert tracker.state.activation_absolute_candle_index == 294


def test_activation_exactly_after_wait_candles() -> None:
    from research.backtests.recovery_bot_shim import RecoveryBotTracker

    tracker = RecoveryBotTracker(config=RecoveryBotConfig(enabled=True, recovery_wait_candles=144))
    tracker.state.reference_reached = True
    tracker.state.reference_absolute_candle_index = 100
    tracker.state.activation_absolute_candle_index = 244
    assert should_activate_recovery(tracker, absolute_candle_index=243, trade_still_open=True) is False
    assert should_activate_recovery(tracker, absolute_candle_index=244, trade_still_open=True) is True


def test_closed_trade_prevents_recovery_activation() -> None:
    from research.backtests.recovery_bot_shim import RecoveryBotTracker

    tracker = RecoveryBotTracker(config=RecoveryBotConfig(enabled=True, recovery_wait_candles=3))
    tracker.state.reference_reached = True
    tracker.state.activation_absolute_candle_index = 20
    assert should_activate_recovery(tracker, absolute_candle_index=25, trade_still_open=False) is False


def test_recovery_only_once_per_trade() -> None:
    from research.backtests.recovery_bot_shim import RecoveryBotTracker

    tracker = RecoveryBotTracker(config=RecoveryBotConfig(enabled=True, recovery_wait_candles=3))
    tracker.state.reference_reached = True
    tracker.state.activation_absolute_candle_index = 20
    tracker.state.recovery_activated = True
    assert should_activate_recovery(tracker, absolute_candle_index=30, trade_still_open=True) is False


def test_aggregate_counts_recovery_closed_without_final_status_closed() -> None:
    from research.backtests.continuous_reentry_backtest import aggregate_continuous_results

    run = BacktestResult(
        symbol="APTUSDT",
        direction="long",
        fill_model="conservative",
        config_source="live",
        trade_number=1,
        realized_pnl=-0.65,
        final_status="closed_negative_pnl",
        exit_reason="recovery_joint_exit",
        recovery_activated=True,
        recovery_gap_fully_closed=True,
        recovery_duration_candles=100,
    )
    agg = aggregate_continuous_results([run])[0]
    assert agg["recovery_closed_count"] == 1
    assert agg["total_recovery_trade_pnl"] == pytest.approx(-0.65)


def test_resolve_recovery_cli_flags() -> None:
    import argparse

    from research.backtests.run_original_hedge_backtest import resolve_recovery_bot_config

    args = argparse.Namespace(
        recovery_bot=True,
        no_recovery_bot=False,
        recovery_start_purpose="CYCLE_4_LONG_ADD",
        recovery_wait_candles=144,
        recovery_bot_config_json=None,
    )
    cfg = resolve_recovery_bot_config(args)
    assert cfg is not None
    assert cfg.enabled is True
    assert cfg.recovery_start_purpose == "CYCLE_4_LONG_ADD"
    assert cfg.recovery_wait_candles == 144

    off_args = argparse.Namespace(
        recovery_bot=False,
        no_recovery_bot=True,
        recovery_start_purpose=None,
        recovery_wait_candles=None,
        recovery_bot_config_json=None,
    )
    assert resolve_recovery_bot_config(off_args) is None


def test_runtime_pnl_matches_offline_simulate() -> None:
    from research.backtests.long_gap_reduction import LongGapReductionConfig, simulate_long_gap_reduction

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = []
    prices = [100.0, 99.0, 98.01, 97.0299, 96.059601, 95.0]
    for index, price in enumerate(prices):
        candles.append(_candle(base + timedelta(minutes=5 * index), price, low=price))

    cfg = LongGapReductionConfig(
        num_steps=4,
        gap_reduce_fraction_per_step=0.25,
        fee_rate=0.00055,
    )
    _events, offline = simulate_long_gap_reduction(
        candles=candles,
        start_local_candle_index=0,
        absolute_start_index=0,
        initial_long_qty=10.0,
        initial_short_qty=6.0,
        long_avg=100.0,
        short_avg=100.0,
        reference_price=100.0,
        base_main_realized_pnl=-1.0,
        cfg=cfg,
    )
    runtime = LongGapReductionRuntime(
        initial_long_qty=10.0,
        initial_short_qty=6.0,
        long_avg=100.0,
        short_avg=100.0,
        reference_price=100.0,
        base_main_realized_pnl=-1.0,
        cfg=cfg,
    )
    runtime.start_event(candles[0], local_candle_index=0, absolute_candle_index=0)
    for index, candle in enumerate(candles[1:], start=1):
        runtime.process_candle(candle, local_candle_index=index, absolute_candle_index=index)
    assert runtime.summary()["total_gap_reduction_net_pnl"] == pytest.approx(
        offline["total_gap_reduction_net_pnl"]
    )
    assert runtime.summary()["total_reduced_qty"] == pytest.approx(offline["total_reduced_qty"])


"""Tests for backtest-only recovery timeout close_all alternative."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from research.backtests.recovery_bot_config import (
    RecoveryBotConfig,
    config_from_mapping,
    default_recovery_bot_config,
    normalize_recovery_timeout_action,
)
from research.backtests.recovery_bot_shim import (
    CLOSE_REASON_ADDITIONAL_LOSS,
    CLOSE_REASON_MAX_LOSS,
    CLOSE_REASON_TIMEOUT,
    EXIT_REASON_RECOVERY_MAX_ADDITIONAL_LOSS_CLOSE,
    EXIT_REASON_RECOVERY_MAX_LOSS_CLOSE,
    EXIT_REASON_RECOVERY_TIMEOUT_CLOSE,
    RecoveryBotTracker,
    estimate_timeout_close_economics,
    process_recovery_bot_after_normal_candle,
    should_activate_recovery,
    should_execute_additional_loss_close,
    should_execute_max_loss_close,
    should_execute_timeout_close,
    _execute_timeout_close_all,
    _note_reference_from_fills,
)
from research.backtests.simulated_execution import resolve_simulated_fee_rate
from research.backtests.simulated_order_book import SimulatedOrderBook, SyntheticCandle
from research.backtests.simulated_pnl import closed_pnl_for_virtual_order_fill
from fixed_cycle_hedge_bot.models import FillEvent


FEE = resolve_simulated_fee_rate()


def _candle(ts: datetime, close: float) -> SyntheticCandle:
    return SyntheticCandle(
        symbol="TESTUSDT",
        timestamp=ts,
        open=close,
        high=close,
        low=close,
        close=close,
    )


def _fill(purpose: str, price: float = 1.0, qty: float = 1.0) -> FillEvent:
    return FillEvent(
        exchange_order_id="ex",
        client_order_id=None,
        side="sell",
        purpose=purpose,
        exec_qty=qty,
        exec_price=price,
        order_type="Market",
        reduce_only=True,
        status="FILLED",
    )


def _sim(long_qty: float, short_qty: float, long_avg: float, short_avg: float, fee_rate: float = FEE):
    book = SimulatedOrderBook(symbol="TESTUSDT", fee_rate=fee_rate)
    book.long_qty = long_qty
    book.short_qty = short_qty
    book.long_avg = long_avg
    book.short_avg = short_avg
    sim = SimpleNamespace(
        book=book,
        config=SimpleNamespace(order_fee_rate_pct=0.055),
        intent_filter=None,
    )
    return sim


def test_default_timeout_action_is_gap_reduction() -> None:
    cfg = default_recovery_bot_config()
    assert cfg.recovery_timeout_action == "gap_reduction"
    assert cfg.recovery_timeout_min_loss_usdt is None
    assert cfg.recovery_max_loss_usdt is None
    assert cfg.recovery_max_additional_loss_usdt is None
    assert normalize_recovery_timeout_action(None) == "gap_reduction"


def test_config_from_mapping_accepts_close_all_and_min_loss() -> None:
    cfg = config_from_mapping(
        {
            "enabled": True,
            "recovery_timeout_action": "close_all",
            "recovery_timeout_min_loss_usdt": 0.5,
            "recovery_max_loss_usdt": 1.0,
            "recovery_wait_candles": 244,
        }
    )
    assert cfg.recovery_timeout_action == "close_all"
    assert cfg.recovery_timeout_min_loss_usdt == pytest.approx(0.5)
    assert cfg.recovery_max_loss_usdt == pytest.approx(1.0)
    assert cfg.recovery_wait_candles == 244


def test_close_all_flattens_long_and_short() -> None:
    tracker = RecoveryBotTracker(
        config=RecoveryBotConfig(
            enabled=True,
            recovery_timeout_action="close_all",
            recovery_wait_candles=2,
            recovery_start_purpose="CYCLE_4_LONG_ADD",
        )
    )
    tracker.state.reference_reached = True
    tracker.state.reference_absolute_candle_index = 10
    tracker.state.activation_absolute_candle_index = 12
    sim = _sim(long_qty=10.0, short_qty=4.0, long_avg=2.0, short_avg=2.0)
    candle = _candle(datetime(2026, 1, 1, tzinfo=timezone.utc), 1.9)
    pnl_delta, closed = process_recovery_bot_after_normal_candle(
        tracker,
        sim,
        result=SimpleNamespace(),
        candle=candle,
        local_candle_index=12,
        absolute_candle_index=12,
        candle_fills=[],
        cumulative_pnl=-0.2,
        trade_still_open=True,
    )
    assert closed is True
    assert sim.book.long_qty == pytest.approx(0.0)
    assert sim.book.short_qty == pytest.approx(0.0)
    assert tracker.state.timeout_close_triggered is True
    assert tracker.state.gap_reduction_skipped is True
    assert tracker.state.exit_reason == EXIT_REASON_RECOVERY_TIMEOUT_CLOSE
    assert tracker.state.gap_runtime is None
    assert pnl_delta != 0.0


def test_closing_fees_both_sides() -> None:
    economics = estimate_timeout_close_economics(
        long_qty=10.0,
        short_qty=5.0,
        long_avg=2.0,
        short_avg=2.0,
        execution_price=1.9,
        realized_pnl_before_close=0.0,
        fee_rate=FEE,
    )
    long_net, long_details = closed_pnl_for_virtual_order_fill(
        side="long",
        reduce_only=True,
        avg_entry_price=2.0,
        fill_price=1.9,
        qty=10.0,
        fee_rate=FEE,
    )
    short_net, short_details = closed_pnl_for_virtual_order_fill(
        side="short",
        reduce_only=True,
        avg_entry_price=2.0,
        fill_price=1.9,
        qty=5.0,
        fee_rate=FEE,
    )
    assert economics["long_closing_fee"] == pytest.approx(
        float(long_details["entry_fee"]) + float(long_details["exit_fee"])
    )
    assert economics["short_closing_fee"] == pytest.approx(
        float(short_details["entry_fee"]) + float(short_details["exit_fee"])
    )
    assert economics["joint_exit_net_pnl"] == pytest.approx(float(long_net) + float(short_net))


def test_realized_pnl_included_exactly_once() -> None:
    economics = estimate_timeout_close_economics(
        long_qty=8.0,
        short_qty=4.0,
        long_avg=1.5,
        short_avg=1.5,
        execution_price=1.4,
        realized_pnl_before_close=-0.35,
        fee_rate=FEE,
    )
    assert economics["net_pnl_after_close"] == pytest.approx(
        -0.35 + economics["joint_exit_net_pnl"]
    )
    assert economics["realized_pnl_before_close"] == pytest.approx(-0.35)


def test_no_gap_reduction_events_after_timeout_close() -> None:
    tracker = RecoveryBotTracker(
        config=RecoveryBotConfig(enabled=True, recovery_timeout_action="close_all", recovery_wait_candles=1)
    )
    tracker.state.reference_reached = True
    tracker.state.activation_absolute_candle_index = 5
    sim = _sim(6.0, 3.0, 1.0, 1.0)
    candle = _candle(datetime(2026, 1, 1, tzinfo=timezone.utc), 0.95)
    process_recovery_bot_after_normal_candle(
        tracker,
        sim,
        result=SimpleNamespace(),
        candle=candle,
        local_candle_index=5,
        absolute_candle_index=5,
        candle_fills=[],
        cumulative_pnl=0.0,
        trade_still_open=True,
    )
    purposes = [event["purpose"] for event in tracker.diagnostic_events]
    assert "RECOVERY_TIMEOUT_CLOSE_ALL" in purposes
    assert not any(purpose.startswith("RECOVERY_GAP_REDUCE") for purpose in purposes)
    assert tracker.state.gap_runtime is None


def test_trade_flat_and_closed_after_timeout() -> None:
    tracker = RecoveryBotTracker(
        config=RecoveryBotConfig(enabled=True, recovery_timeout_action="close_all", recovery_wait_candles=0)
    )
    tracker.state.reference_reached = True
    tracker.state.activation_absolute_candle_index = 1
    sim = _sim(2.0, 1.0, 1.0, 1.0)
    candle = _candle(datetime(2026, 1, 1, tzinfo=timezone.utc), 1.0)
    _, closed = process_recovery_bot_after_normal_candle(
        tracker,
        sim,
        result=SimpleNamespace(),
        candle=candle,
        local_candle_index=1,
        absolute_candle_index=1,
        candle_fills=[],
        cumulative_pnl=0.1,
        trade_still_open=True,
    )
    assert closed is True
    assert tracker.state.recovery_completed is True
    assert sim.book.long_qty == 0.0
    assert sim.book.short_qty == 0.0


def test_min_loss_triggers_at_exact_boundary() -> None:
    # Craft economics so net_after == -0.50 exactly is hard; test the gate helper.
    assert should_execute_timeout_close(estimated_net_pnl_after_close=-0.50, min_loss_usdt=0.50) is True
    assert should_execute_timeout_close(estimated_net_pnl_after_close=-0.5000001, min_loss_usdt=0.50) is True


def test_min_loss_does_not_trigger_when_loss_smaller() -> None:
    assert should_execute_timeout_close(estimated_net_pnl_after_close=-0.49, min_loss_usdt=0.50) is False


def test_positive_pnl_not_closed_with_positive_min_loss() -> None:
    assert should_execute_timeout_close(estimated_net_pnl_after_close=0.25, min_loss_usdt=0.50) is False
    assert should_execute_timeout_close(estimated_net_pnl_after_close=0.25, min_loss_usdt=None) is True


def test_min_loss_skip_continues_trade_without_reeval() -> None:
    tracker = RecoveryBotTracker(
        config=RecoveryBotConfig(
            enabled=True,
            recovery_timeout_action="close_all",
            recovery_timeout_min_loss_usdt=100.0,
            recovery_wait_candles=1,
        )
    )
    tracker.state.reference_reached = True
    tracker.state.activation_absolute_candle_index = 3
    sim = _sim(2.0, 1.0, 1.0, 1.0)
    candle = _candle(datetime(2026, 1, 1, tzinfo=timezone.utc), 1.0)
    _, closed = process_recovery_bot_after_normal_candle(
        tracker,
        sim,
        result=SimpleNamespace(),
        candle=candle,
        local_candle_index=3,
        absolute_candle_index=3,
        candle_fills=[],
        cumulative_pnl=0.0,
        trade_still_open=True,
    )
    assert closed is False
    assert tracker.state.timeout_action_evaluated is True
    assert tracker.state.timeout_close_skipped is True
    assert sim.book.long_qty == pytest.approx(2.0)
    # Later candles must not re-trigger.
    assert should_activate_recovery(tracker, absolute_candle_index=10, trade_still_open=True) is False
    _, closed_again = process_recovery_bot_after_normal_candle(
        tracker,
        sim,
        result=SimpleNamespace(),
        candle=candle,
        local_candle_index=10,
        absolute_candle_index=10,
        candle_fills=[],
        cumulative_pnl=0.0,
        trade_still_open=True,
    )
    assert closed_again is False
    assert tracker.state.timeout_close_triggered is False


def test_no_double_timeout_close() -> None:
    tracker = RecoveryBotTracker(
        config=RecoveryBotConfig(enabled=True, recovery_timeout_action="close_all", recovery_wait_candles=0)
    )
    tracker.state.reference_reached = True
    tracker.state.activation_absolute_candle_index = 1
    sim = _sim(4.0, 2.0, 1.0, 1.0)
    candle = _candle(datetime(2026, 1, 1, tzinfo=timezone.utc), 0.9)
    process_recovery_bot_after_normal_candle(
        tracker,
        sim,
        result=SimpleNamespace(),
        candle=candle,
        local_candle_index=1,
        absolute_candle_index=1,
        candle_fills=[],
        cumulative_pnl=0.0,
        trade_still_open=True,
    )
    first_events = len(tracker.diagnostic_events)
    process_recovery_bot_after_normal_candle(
        tracker,
        sim,
        result=SimpleNamespace(),
        candle=candle,
        local_candle_index=2,
        absolute_candle_index=2,
        candle_fills=[],
        cumulative_pnl=0.0,
        trade_still_open=False,
    )
    assert sum(1 for e in tracker.diagnostic_events if e["purpose"] == "RECOVERY_TIMEOUT_CLOSE_ALL") == 1
    assert len(tracker.diagnostic_events) == first_events


def test_short_direction_close_all_mirrored() -> None:
    """Short-primary still flattens both legs; qty roles may be inverted."""
    tracker = RecoveryBotTracker(
        config=RecoveryBotConfig(
            enabled=True,
            recovery_timeout_action="close_all",
            recovery_start_purpose="CYCLE_4_SHORT_REDUCE",
            recovery_wait_candles=0,
        )
    )
    tracker.state.reference_reached = True
    tracker.state.activation_absolute_candle_index = 1
    # Short-primary style inventory: short larger than long.
    sim = _sim(long_qty=5.0, short_qty=10.0, long_avg=1.2, short_avg=1.2)
    candle = _candle(datetime(2026, 1, 1, tzinfo=timezone.utc), 1.25)
    _, closed = process_recovery_bot_after_normal_candle(
        tracker,
        sim,
        result=SimpleNamespace(),
        candle=candle,
        local_candle_index=1,
        absolute_candle_index=1,
        candle_fills=[],
        cumulative_pnl=-0.1,
        trade_still_open=True,
    )
    assert closed is True
    assert sim.book.long_qty == 0.0
    assert sim.book.short_qty == 0.0
    event = tracker.state.timeout_close_event
    assert event is not None
    assert event["long_qty_before"] == pytest.approx(5.0)
    assert event["short_qty_before"] == pytest.approx(10.0)


def test_gap_reduction_default_still_activates_runtime() -> None:
    tracker = RecoveryBotTracker(
        config=RecoveryBotConfig(
            enabled=True,
            recovery_timeout_action="gap_reduction",
            recovery_wait_candles=0,
        )
    )
    tracker.state.reference_reached = True
    tracker.state.activation_absolute_candle_index = 1
    sim = _sim(8.0, 4.0, 1.0, 1.0)
    candle = _candle(datetime(2026, 1, 1, tzinfo=timezone.utc), 1.0)
    process_recovery_bot_after_normal_candle(
        tracker,
        sim,
        result=SimpleNamespace(),
        candle=candle,
        local_candle_index=1,
        absolute_candle_index=1,
        candle_fills=[],
        cumulative_pnl=0.0,
        trade_still_open=True,
    )
    assert tracker.state.recovery_activated is True
    assert tracker.state.gap_runtime is not None
    assert tracker.state.timeout_close_triggered is False
    assert tracker.state.gap_reduction_skipped is False


def test_reference_fill_arms_wait_for_close_all() -> None:
    tracker = RecoveryBotTracker(
        config=RecoveryBotConfig(
            enabled=True,
            recovery_timeout_action="close_all",
            recovery_wait_candles=244,
            recovery_start_purpose="CYCLE_4_LONG_ADD",
        )
    )
    _note_reference_from_fills(
        tracker,
        fills=[_fill("CYCLE_4_LONG_ADD")],
        local_candle_index=40,
        absolute_candle_index=100,
        timestamp="2026-01-01T00:00:00+00:00",
    )
    assert tracker.state.activation_absolute_candle_index == 344
    assert should_activate_recovery(tracker, absolute_candle_index=343, trade_still_open=True) is False
    assert should_activate_recovery(tracker, absolute_candle_index=344, trade_still_open=True) is True


def test_cli_close_all_enables_recovery_without_recovery_bot_flag() -> None:
    from argparse import Namespace

    from research.backtests.run_original_hedge_backtest import resolve_recovery_bot_config

    args = Namespace(
        recovery_bot_config_json=None,
        no_recovery_bot=False,
        recovery_bot=False,
        recovery_start_purpose="CYCLE_4_LONG_ADD",
        recovery_wait_candles=576,
        recovery_timeout_action="close_all",
        recovery_timeout_min_loss_usdt=None,
    )
    cfg = resolve_recovery_bot_config(args)
    assert cfg is not None
    assert cfg.enabled is True
    assert cfg.recovery_timeout_action == "close_all"
    assert cfg.recovery_wait_candles == 576
    assert cfg.recovery_start_purpose == "CYCLE_4_LONG_ADD"


def test_cli_gap_reduction_defaults_remain_disabled_without_flag() -> None:
    from argparse import Namespace

    from research.backtests.run_original_hedge_backtest import resolve_recovery_bot_config

    args = Namespace(
        recovery_bot_config_json=None,
        no_recovery_bot=False,
        recovery_bot=False,
        recovery_start_purpose="CYCLE_4_LONG_ADD",
        recovery_wait_candles=144,
        recovery_timeout_action="gap_reduction",
        recovery_timeout_min_loss_usdt=None,
    )
    assert resolve_recovery_bot_config(args) is None


def test_close_all_fires_exactly_at_wait_end_candle() -> None:
    """Reference at X, no close at X+575, exactly one close_all at X+576."""
    wait = 576
    ref_abs = 100
    target = ref_abs + wait
    tracker = RecoveryBotTracker(
        config=RecoveryBotConfig(
            enabled=True,
            recovery_timeout_action="close_all",
            recovery_wait_candles=wait,
            recovery_start_purpose="CYCLE_4_LONG_ADD",
        )
    )
    _note_reference_from_fills(
        tracker,
        fills=[_fill("CYCLE_4_LONG_ADD")],
        local_candle_index=ref_abs,
        absolute_candle_index=ref_abs,
        timestamp="2026-01-01T00:00:00+00:00",
    )
    assert tracker.state.planned_timeout_absolute_candle_index == target
    sim = _sim(long_qty=10.0, short_qty=5.0, long_avg=1.5, short_avg=1.5)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    pre = _candle(base + timedelta(minutes=5 * (target - 1)), 1.4)
    pnl_pre, closed_pre = process_recovery_bot_after_normal_candle(
        tracker,
        sim,
        result=SimpleNamespace(),
        candle=pre,
        local_candle_index=target - 1,
        absolute_candle_index=target - 1,
        candle_fills=[],
        cumulative_pnl=-0.1,
        trade_still_open=True,
    )
    assert closed_pre is False
    assert tracker.state.timeout_close_triggered is False
    assert sim.book.long_qty == pytest.approx(10.0)
    assert pnl_pre == 0.0

    at = _candle(base + timedelta(minutes=5 * target), 1.4)
    pnl_at, closed_at = process_recovery_bot_after_normal_candle(
        tracker,
        sim,
        result=SimpleNamespace(),
        candle=at,
        local_candle_index=target,
        absolute_candle_index=target,
        candle_fills=[],
        cumulative_pnl=-0.1,
        trade_still_open=True,
    )
    assert closed_at is True
    assert tracker.state.timeout_close_triggered is True
    assert tracker.state.exit_reason == EXIT_REASON_RECOVERY_TIMEOUT_CLOSE
    assert tracker.state.close_reason == CLOSE_REASON_TIMEOUT
    assert tracker.state.max_loss_triggered is False
    assert sim.book.long_qty == 0.0
    assert sim.book.short_qty == 0.0
    assert tracker.state.gap_runtime is None
    assert pnl_at != 0.0
    assert tracker.state.timeout_close_fees is not None
    assert tracker.state.timeout_close_fees > 0.0

    post = _candle(base + timedelta(minutes=5 * (target + 1)), 1.4)
    _, closed_post = process_recovery_bot_after_normal_candle(
        tracker,
        sim,
        result=SimpleNamespace(),
        candle=post,
        local_candle_index=target + 1,
        absolute_candle_index=target + 1,
        candle_fills=[],
        cumulative_pnl=-0.1,
        trade_still_open=False,
    )
    assert closed_post is False
    assert sum(
        1 for event in tracker.diagnostic_events if event["purpose"] == "RECOVERY_TIMEOUT_CLOSE_ALL"
    ) == 1
    assert sum(
        1 for event in tracker.diagnostic_events if event["purpose"] == "RECOVERY_MAX_LOSS_CLOSE_ALL"
    ) == 0


def test_populate_timeout_diagnostic_fields() -> None:
    from research.backtests.backtest_report import BacktestResult
    from research.backtests.recovery_bot_shim import populate_recovery_bot_result_fields

    tracker = RecoveryBotTracker(
        config=RecoveryBotConfig(
            enabled=True,
            recovery_timeout_action="close_all",
            recovery_wait_candles=576,
            recovery_start_purpose="CYCLE_4_LONG_ADD",
            recovery_max_loss_usdt=1.0,
        )
    )
    tracker.state.reference_reached = True
    tracker.state.reference_absolute_candle_index = 50
    tracker.state.planned_timeout_absolute_candle_index = 626
    tracker.state.activation_absolute_candle_index = 626
    tracker.state.timeout_close_triggered = True
    tracker.state.close_reason = CLOSE_REASON_TIMEOUT
    tracker.state.recovery_exit_absolute_candle_index = 626
    tracker.state.timeout_estimated_net_exit_pnl = -1.25
    tracker.state.timeout_close_net_pnl = -1.25
    tracker.state.gap_reduction_skipped = True

    result = BacktestResult(symbol="APTUSDT", direction="long")
    populate_recovery_bot_result_fields(result, tracker)
    assert result.recovery_timeout_action == "close_all"
    assert result.recovery_reference_purpose == "CYCLE_4_LONG_ADD"
    assert result.recovery_reference_fill_candle_index == 50
    assert result.recovery_timeout_target_candle_index == 626
    assert result.recovery_timeout_triggered is True
    assert result.recovery_timeout_trigger_candle_index == 626
    assert result.recovery_timeout_skip_reason is None
    assert result.recovery_timeout_estimated_net_exit_pnl == pytest.approx(-1.25)
    assert result.recovery_gap_reduction_skipped is True
    assert result.recovery_max_loss_usdt == pytest.approx(1.0)
    assert result.recovery_max_loss_triggered is False
    assert result.recovery_close_reason == CLOSE_REASON_TIMEOUT


def _economics(net: float) -> dict[str, float]:
    return {
        "realized_pnl_before_close": 0.0,
        "unrealized_long_pnl": 0.0,
        "unrealized_short_pnl": 0.0,
        "combined_unrealized_pnl": 0.0,
        "long_closing_fee": 0.01,
        "short_closing_fee": 0.01,
        "total_closing_fee": 0.02,
        "long_net_close_pnl": net / 2.0,
        "short_net_close_pnl": net / 2.0,
        "joint_exit_net_pnl": net,
        "net_pnl_after_close": net,
    }


def test_should_execute_max_loss_boundary() -> None:
    assert should_execute_max_loss_close(
        estimated_net_pnl_after_close=-0.99, max_loss_usdt=1.0
    ) is False
    assert should_execute_max_loss_close(
        estimated_net_pnl_after_close=-1.00, max_loss_usdt=1.0
    ) is True
    assert should_execute_max_loss_close(
        estimated_net_pnl_after_close=-1.04, max_loss_usdt=1.0
    ) is True


def test_max_loss_boundary_closes_at_minus_one_not_minus_point_nine_nine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from research.backtests import recovery_bot_shim as shim

    tracker = RecoveryBotTracker(
        config=RecoveryBotConfig(
            enabled=True,
            recovery_timeout_action="close_all",
            recovery_wait_candles=576,
            recovery_max_loss_usdt=1.0,
            recovery_start_purpose="CYCLE_4_LONG_ADD",
        )
    )
    _note_reference_from_fills(
        tracker,
        fills=[_fill("CYCLE_4_LONG_ADD")],
        local_candle_index=100,
        absolute_candle_index=100,
        timestamp="2026-01-01T00:00:00+00:00",
    )
    sim = _sim(long_qty=10.0, short_qty=5.0, long_avg=1.5, short_avg=1.5)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    monkeypatch.setattr(
        shim, "estimate_timeout_close_economics", lambda **_kwargs: _economics(-0.99)
    )
    _, closed = process_recovery_bot_after_normal_candle(
        tracker,
        sim,
        result=SimpleNamespace(),
        candle=_candle(base + timedelta(minutes=5), 1.4),
        local_candle_index=101,
        absolute_candle_index=101,
        candle_fills=[],
        cumulative_pnl=0.0,
        trade_still_open=True,
    )
    assert closed is False
    assert tracker.state.max_loss_triggered is False
    assert sim.book.long_qty == pytest.approx(10.0)

    monkeypatch.setattr(
        shim, "estimate_timeout_close_economics", lambda **_kwargs: _economics(-1.00)
    )
    pnl, closed = process_recovery_bot_after_normal_candle(
        tracker,
        sim,
        result=SimpleNamespace(),
        candle=_candle(base + timedelta(minutes=10), 1.4),
        local_candle_index=102,
        absolute_candle_index=102,
        candle_fills=[],
        cumulative_pnl=0.0,
        trade_still_open=True,
    )
    assert closed is True
    assert tracker.state.max_loss_triggered is True
    assert tracker.state.timeout_close_triggered is False
    assert tracker.state.exit_reason == EXIT_REASON_RECOVERY_MAX_LOSS_CLOSE
    assert tracker.state.close_reason == CLOSE_REASON_MAX_LOSS
    assert tracker.state.max_loss_trigger_candle_index == 102
    assert tracker.state.max_loss_estimated_net_exit_pnl == pytest.approx(-1.00)
    assert sim.book.long_qty == 0.0
    assert sim.book.short_qty == 0.0
    assert tracker.state.gap_runtime is None
    assert tracker.state.timeout_close_fees is not None
    assert tracker.state.timeout_close_fees > 0.0
    assert pnl == pytest.approx(-1.00)


def test_max_loss_fires_before_timeout_and_blocks_later_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from research.backtests import recovery_bot_shim as shim

    wait = 576
    ref = 100
    tracker = RecoveryBotTracker(
        config=RecoveryBotConfig(
            enabled=True,
            recovery_timeout_action="close_all",
            recovery_wait_candles=wait,
            recovery_max_loss_usdt=1.0,
        )
    )
    _note_reference_from_fills(
        tracker,
        fills=[_fill("CYCLE_4_LONG_ADD")],
        local_candle_index=ref,
        absolute_candle_index=ref,
        timestamp="2026-01-01T00:00:00+00:00",
    )
    sim = _sim(long_qty=8.0, short_qty=3.0, long_avg=1.5, short_avg=1.5)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    early = ref + 50

    monkeypatch.setattr(
        shim, "estimate_timeout_close_economics", lambda **_kwargs: _economics(-1.04)
    )
    _, closed = process_recovery_bot_after_normal_candle(
        tracker,
        sim,
        result=SimpleNamespace(),
        candle=_candle(base + timedelta(minutes=5 * early), 1.3),
        local_candle_index=early,
        absolute_candle_index=early,
        candle_fills=[],
        cumulative_pnl=-0.2,
        trade_still_open=True,
    )
    assert closed is True
    assert tracker.state.max_loss_triggered is True
    assert tracker.state.exit_reason == EXIT_REASON_RECOVERY_MAX_LOSS_CLOSE

    # Later timeout candle must not fire again.
    monkeypatch.setattr(
        shim, "estimate_timeout_close_economics", lambda **_kwargs: _economics(-5.0)
    )
    _, closed_later = process_recovery_bot_after_normal_candle(
        tracker,
        sim,
        result=SimpleNamespace(),
        candle=_candle(base + timedelta(minutes=5 * (ref + wait)), 1.0),
        local_candle_index=ref + wait,
        absolute_candle_index=ref + wait,
        candle_fills=[],
        cumulative_pnl=-0.2,
        trade_still_open=False,
    )
    assert closed_later is False
    assert tracker.state.timeout_close_triggered is False
    assert sum(
        1 for event in tracker.diagnostic_events if event["purpose"] == "RECOVERY_MAX_LOSS_CLOSE_ALL"
    ) == 1
    assert sum(
        1 for event in tracker.diagnostic_events if event["purpose"] == "RECOVERY_TIMEOUT_CLOSE_ALL"
    ) == 0


def test_max_loss_not_reached_falls_back_to_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from research.backtests import recovery_bot_shim as shim

    wait = 576
    ref = 100
    target = ref + wait
    tracker = RecoveryBotTracker(
        config=RecoveryBotConfig(
            enabled=True,
            recovery_timeout_action="close_all",
            recovery_wait_candles=wait,
            recovery_max_loss_usdt=1.0,
        )
    )
    _note_reference_from_fills(
        tracker,
        fills=[_fill("CYCLE_4_LONG_ADD")],
        local_candle_index=ref,
        absolute_candle_index=ref,
        timestamp="2026-01-01T00:00:00+00:00",
    )
    sim = _sim(long_qty=10.0, short_qty=5.0, long_avg=1.5, short_avg=1.5)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    monkeypatch.setattr(
        shim, "estimate_timeout_close_economics", lambda **_kwargs: _economics(-0.25)
    )
    _, closed_mid = process_recovery_bot_after_normal_candle(
        tracker,
        sim,
        result=SimpleNamespace(),
        candle=_candle(base + timedelta(minutes=5 * (target - 1)), 1.4),
        local_candle_index=target - 1,
        absolute_candle_index=target - 1,
        candle_fills=[],
        cumulative_pnl=0.0,
        trade_still_open=True,
    )
    assert closed_mid is False
    assert tracker.state.max_loss_triggered is False

    _, closed_at = process_recovery_bot_after_normal_candle(
        tracker,
        sim,
        result=SimpleNamespace(),
        candle=_candle(base + timedelta(minutes=5 * target), 1.4),
        local_candle_index=target,
        absolute_candle_index=target,
        candle_fills=[],
        cumulative_pnl=0.0,
        trade_still_open=True,
    )
    assert closed_at is True
    assert tracker.state.timeout_close_triggered is True
    assert tracker.state.max_loss_triggered is False
    assert tracker.state.exit_reason == EXIT_REASON_RECOVERY_TIMEOUT_CLOSE
    assert tracker.state.close_reason == CLOSE_REASON_TIMEOUT
    assert sim.book.long_qty == 0.0
    assert sim.book.short_qty == 0.0


def test_max_loss_candle_jump_stores_actual_estimated_pnl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from research.backtests import recovery_bot_shim as shim
    from research.backtests.backtest_report import BacktestResult
    from research.backtests.recovery_bot_shim import populate_recovery_bot_result_fields

    tracker = RecoveryBotTracker(
        config=RecoveryBotConfig(
            enabled=True,
            recovery_timeout_action="close_all",
            recovery_wait_candles=576,
            recovery_max_loss_usdt=1.0,
        )
    )
    _note_reference_from_fills(
        tracker,
        fills=[_fill("CYCLE_4_LONG_ADD")],
        local_candle_index=10,
        absolute_candle_index=10,
        timestamp="2026-01-01T00:00:00+00:00",
    )
    sim = _sim(long_qty=10.0, short_qty=5.0, long_avg=1.5, short_avg=1.5)
    jumped = -1.50
    monkeypatch.setattr(
        shim, "estimate_timeout_close_economics", lambda **_kwargs: _economics(jumped)
    )
    process_recovery_bot_after_normal_candle(
        tracker,
        sim,
        result=SimpleNamespace(),
        candle=_candle(datetime(2026, 1, 1, tzinfo=timezone.utc), 1.0),
        local_candle_index=11,
        absolute_candle_index=11,
        candle_fills=[],
        cumulative_pnl=0.0,
        trade_still_open=True,
    )
    assert tracker.state.max_loss_triggered is True
    assert tracker.state.max_loss_estimated_net_exit_pnl == pytest.approx(jumped)
    assert jumped < -1.0

    result = BacktestResult(symbol="APTUSDT", direction="long")
    populate_recovery_bot_result_fields(result, tracker)
    assert result.recovery_max_loss_usdt == pytest.approx(1.0)
    assert result.recovery_max_loss_triggered is True
    assert result.recovery_max_loss_trigger_candle_index == 11
    assert result.recovery_max_loss_estimated_net_exit_pnl == pytest.approx(jumped)
    assert result.recovery_close_reason == CLOSE_REASON_MAX_LOSS


def test_cli_max_loss_enables_recovery_without_recovery_bot_flag() -> None:
    from argparse import Namespace

    from research.backtests.run_original_hedge_backtest import resolve_recovery_bot_config

    args = Namespace(
        recovery_bot_config_json=None,
        no_recovery_bot=False,
        recovery_bot=False,
        recovery_start_purpose="CYCLE_4_LONG_ADD",
        recovery_wait_candles=576,
        recovery_timeout_action="close_all",
        recovery_timeout_min_loss_usdt=None,
        recovery_max_loss_usdt=1.0,
    )
    cfg = resolve_recovery_bot_config(args)
    assert cfg is not None
    assert cfg.enabled is True
    assert cfg.recovery_max_loss_usdt == pytest.approx(1.0)
    assert cfg.recovery_timeout_action == "close_all"

    # Max-loss alone (even with default gap_reduction timeout) must enable recovery.
    args_only_max = Namespace(
        recovery_bot_config_json=None,
        no_recovery_bot=False,
        recovery_bot=False,
        recovery_start_purpose="CYCLE_4_LONG_ADD",
        recovery_wait_candles=576,
        recovery_timeout_action="gap_reduction",
        recovery_timeout_min_loss_usdt=None,
        recovery_max_loss_usdt=1.0,
    )
    cfg_only = resolve_recovery_bot_config(args_only_max)
    assert cfg_only is not None
    assert cfg_only.enabled is True
    assert cfg_only.recovery_max_loss_usdt == pytest.approx(1.0)


def test_should_execute_additional_loss_boundary() -> None:
    assert should_execute_additional_loss_close(
        reference_net_exit_pnl=-0.35,
        current_estimated_net_exit_pnl=-1.04,
        max_additional_loss_usdt=0.70,
    ) is False  # additional = 0.69
    assert should_execute_additional_loss_close(
        reference_net_exit_pnl=-0.35,
        current_estimated_net_exit_pnl=-1.05,
        max_additional_loss_usdt=0.70,
    ) is True  # additional = 0.70
    assert should_execute_additional_loss_close(
        reference_net_exit_pnl=-0.35,
        current_estimated_net_exit_pnl=-1.08,
        max_additional_loss_usdt=0.70,
    ) is True  # additional = 0.73


def test_reference_baseline_stored_at_reference_fill() -> None:
    tracker = RecoveryBotTracker(
        config=RecoveryBotConfig(
            enabled=True,
            recovery_timeout_action="close_all",
            recovery_wait_candles=576,
            recovery_max_additional_loss_usdt=0.70,
        )
    )
    sim = _sim(long_qty=10.0, short_qty=5.0, long_avg=1.5, short_avg=1.5)
    candle = _candle(datetime(2026, 1, 1, tzinfo=timezone.utc), 1.4)
    expected_net = estimate_timeout_close_economics(
        long_qty=10.0,
        short_qty=5.0,
        long_avg=1.5,
        short_avg=1.5,
        execution_price=1.4,
        realized_pnl_before_close=-0.10,
        fee_rate=FEE,
    )["net_pnl_after_close"]

    _note_reference_from_fills(
        tracker,
        fills=[_fill("CYCLE_4_LONG_ADD")],
        local_candle_index=50,
        absolute_candle_index=50,
        timestamp="2026-01-01T00:00:00+00:00",
        sim=sim,
        candle=candle,
        cumulative_pnl=-0.10,
    )
    assert tracker.state.reference_reached is True
    assert tracker.state.reference_net_exit_pnl == pytest.approx(expected_net)
    assert tracker.state.current_net_exit_pnl == pytest.approx(expected_net)
    assert tracker.state.additional_loss_usdt == pytest.approx(0.0)


def test_additional_loss_boundary_069_no_exit_070_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from research.backtests import recovery_bot_shim as shim
    from research.backtests.backtest_report import BacktestResult
    from research.backtests.recovery_bot_shim import populate_recovery_bot_result_fields

    baseline = -0.35
    tracker = RecoveryBotTracker(
        config=RecoveryBotConfig(
            enabled=True,
            recovery_timeout_action="close_all",
            recovery_wait_candles=576,
            recovery_max_additional_loss_usdt=0.70,
        )
    )
    tracker.state.reference_reached = True
    tracker.state.reference_absolute_candle_index = 100
    tracker.state.activation_absolute_candle_index = 676
    tracker.state.planned_timeout_absolute_candle_index = 676
    tracker.state.reference_net_exit_pnl = baseline
    sim = _sim(long_qty=10.0, short_qty=5.0, long_avg=1.5, short_avg=1.5)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    monkeypatch.setattr(
        shim, "estimate_timeout_close_economics", lambda **_kwargs: _economics(-1.04)
    )
    _, closed = process_recovery_bot_after_normal_candle(
        tracker,
        sim,
        result=SimpleNamespace(),
        candle=_candle(base + timedelta(minutes=5), 1.3),
        local_candle_index=101,
        absolute_candle_index=101,
        candle_fills=[],
        cumulative_pnl=0.0,
        trade_still_open=True,
    )
    assert closed is False
    assert tracker.state.max_additional_loss_triggered is False
    assert tracker.state.additional_loss_usdt == pytest.approx(0.69)
    assert sim.book.long_qty == pytest.approx(10.0)

    monkeypatch.setattr(
        shim, "estimate_timeout_close_economics", lambda **_kwargs: _economics(-1.05)
    )
    _, closed = process_recovery_bot_after_normal_candle(
        tracker,
        sim,
        result=SimpleNamespace(),
        candle=_candle(base + timedelta(minutes=10), 1.3),
        local_candle_index=102,
        absolute_candle_index=102,
        candle_fills=[],
        cumulative_pnl=0.0,
        trade_still_open=True,
    )
    assert closed is True
    assert tracker.state.max_additional_loss_triggered is True
    assert tracker.state.timeout_close_triggered is False
    assert tracker.state.max_loss_triggered is False
    assert tracker.state.exit_reason == EXIT_REASON_RECOVERY_MAX_ADDITIONAL_LOSS_CLOSE
    assert tracker.state.close_reason == CLOSE_REASON_ADDITIONAL_LOSS
    assert tracker.state.max_additional_loss_trigger_candle_index == 102
    assert tracker.state.max_additional_loss_estimated_net_exit_pnl == pytest.approx(-1.05)
    assert tracker.state.additional_loss_usdt == pytest.approx(0.70)
    assert sim.book.long_qty == 0.0
    assert sim.book.short_qty == 0.0
    assert tracker.state.gap_runtime is None
    assert sum(
        1
        for event in tracker.diagnostic_events
        if event["purpose"] == "RECOVERY_MAX_ADDITIONAL_LOSS_CLOSE_ALL"
    ) == 1

    result = BacktestResult(symbol="APTUSDT", direction="long")
    populate_recovery_bot_result_fields(result, tracker)
    assert result.recovery_max_additional_loss_usdt == pytest.approx(0.70)
    assert result.recovery_reference_net_exit_pnl == pytest.approx(baseline)
    assert result.recovery_current_net_exit_pnl == pytest.approx(-1.05)
    assert result.recovery_additional_loss_usdt == pytest.approx(0.70)
    assert result.recovery_max_additional_loss_triggered is True
    assert result.recovery_max_additional_loss_trigger_candle_index == 102
    assert result.recovery_max_additional_loss_estimated_net_exit_pnl == pytest.approx(-1.05)
    assert result.recovery_close_reason == CLOSE_REASON_ADDITIONAL_LOSS


def test_absolute_and_additional_loss_first_hit_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from research.backtests import recovery_bot_shim as shim

    # Absolute max-loss hits first (current <= -1.50) while additional is also large.
    tracker = RecoveryBotTracker(
        config=RecoveryBotConfig(
            enabled=True,
            recovery_timeout_action="close_all",
            recovery_wait_candles=576,
            recovery_max_loss_usdt=1.50,
            recovery_max_additional_loss_usdt=0.70,
        )
    )
    tracker.state.reference_reached = True
    tracker.state.reference_absolute_candle_index = 10
    tracker.state.activation_absolute_candle_index = 586
    tracker.state.planned_timeout_absolute_candle_index = 586
    tracker.state.reference_net_exit_pnl = -0.20
    sim = _sim(long_qty=10.0, short_qty=5.0, long_avg=1.5, short_avg=1.5)
    monkeypatch.setattr(
        shim, "estimate_timeout_close_economics", lambda **_kwargs: _economics(-1.60)
    )
    process_recovery_bot_after_normal_candle(
        tracker,
        sim,
        result=SimpleNamespace(),
        candle=_candle(datetime(2026, 1, 1, tzinfo=timezone.utc), 1.0),
        local_candle_index=11,
        absolute_candle_index=11,
        candle_fills=[],
        cumulative_pnl=0.0,
        trade_still_open=True,
    )
    assert tracker.state.close_reason == CLOSE_REASON_MAX_LOSS
    assert tracker.state.exit_reason == EXIT_REASON_RECOVERY_MAX_LOSS_CLOSE
    assert tracker.state.max_loss_triggered is True
    assert tracker.state.max_additional_loss_triggered is False

    # Additional-loss hits while absolute max-loss is not yet breached.
    tracker2 = RecoveryBotTracker(
        config=RecoveryBotConfig(
            enabled=True,
            recovery_timeout_action="close_all",
            recovery_wait_candles=576,
            recovery_max_loss_usdt=1.50,
            recovery_max_additional_loss_usdt=0.70,
        )
    )
    tracker2.state.reference_reached = True
    tracker2.state.reference_absolute_candle_index = 10
    tracker2.state.activation_absolute_candle_index = 586
    tracker2.state.planned_timeout_absolute_candle_index = 586
    tracker2.state.reference_net_exit_pnl = -0.35
    sim2 = _sim(long_qty=10.0, short_qty=5.0, long_avg=1.5, short_avg=1.5)
    monkeypatch.setattr(
        shim, "estimate_timeout_close_economics", lambda **_kwargs: _economics(-1.08)
    )
    process_recovery_bot_after_normal_candle(
        tracker2,
        sim2,
        result=SimpleNamespace(),
        candle=_candle(datetime(2026, 1, 1, tzinfo=timezone.utc), 1.0),
        local_candle_index=11,
        absolute_candle_index=11,
        candle_fills=[],
        cumulative_pnl=0.0,
        trade_still_open=True,
    )
    assert tracker2.state.close_reason == CLOSE_REASON_ADDITIONAL_LOSS
    assert tracker2.state.exit_reason == EXIT_REASON_RECOVERY_MAX_ADDITIONAL_LOSS_CLOSE
    assert tracker2.state.max_additional_loss_triggered is True
    assert tracker2.state.max_loss_triggered is False
    assert sim2.book.long_qty == 0.0
    assert sim2.book.short_qty == 0.0


def test_additional_loss_not_reached_falls_back_to_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from research.backtests import recovery_bot_shim as shim

    wait = 576
    ref = 100
    target = ref + wait
    tracker = RecoveryBotTracker(
        config=RecoveryBotConfig(
            enabled=True,
            recovery_timeout_action="close_all",
            recovery_wait_candles=wait,
            recovery_max_additional_loss_usdt=0.70,
            recovery_max_loss_usdt=1.50,
        )
    )
    tracker.state.reference_reached = True
    tracker.state.reference_absolute_candle_index = ref
    tracker.state.activation_absolute_candle_index = target
    tracker.state.planned_timeout_absolute_candle_index = target
    tracker.state.reference_net_exit_pnl = -0.35
    sim = _sim(long_qty=10.0, short_qty=5.0, long_avg=1.5, short_avg=1.5)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    monkeypatch.setattr(
        shim, "estimate_timeout_close_economics", lambda **_kwargs: _economics(-0.50)
    )
    _, closed_mid = process_recovery_bot_after_normal_candle(
        tracker,
        sim,
        result=SimpleNamespace(),
        candle=_candle(base + timedelta(minutes=5 * (target - 1)), 1.4),
        local_candle_index=target - 1,
        absolute_candle_index=target - 1,
        candle_fills=[],
        cumulative_pnl=0.0,
        trade_still_open=True,
    )
    assert closed_mid is False
    assert tracker.state.max_additional_loss_triggered is False

    _, closed_at = process_recovery_bot_after_normal_candle(
        tracker,
        sim,
        result=SimpleNamespace(),
        candle=_candle(base + timedelta(minutes=5 * target), 1.4),
        local_candle_index=target,
        absolute_candle_index=target,
        candle_fills=[],
        cumulative_pnl=0.0,
        trade_still_open=True,
    )
    assert closed_at is True
    assert tracker.state.timeout_close_triggered is True
    assert tracker.state.close_reason == CLOSE_REASON_TIMEOUT
    assert tracker.state.exit_reason == EXIT_REASON_RECOVERY_TIMEOUT_CLOSE
    assert sum(
        1 for event in tracker.diagnostic_events if event["purpose"] == "RECOVERY_TIMEOUT_CLOSE_ALL"
    ) == 1
    assert sum(
        1
        for event in tracker.diagnostic_events
        if event["purpose"] == "RECOVERY_MAX_ADDITIONAL_LOSS_CLOSE_ALL"
    ) == 0


def test_additional_loss_no_double_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    from research.backtests import recovery_bot_shim as shim

    tracker = RecoveryBotTracker(
        config=RecoveryBotConfig(
            enabled=True,
            recovery_timeout_action="close_all",
            recovery_wait_candles=576,
            recovery_max_additional_loss_usdt=0.70,
        )
    )
    tracker.state.reference_reached = True
    tracker.state.reference_absolute_candle_index = 10
    tracker.state.activation_absolute_candle_index = 586
    tracker.state.planned_timeout_absolute_candle_index = 586
    tracker.state.reference_net_exit_pnl = -0.35
    sim = _sim(long_qty=10.0, short_qty=5.0, long_avg=1.5, short_avg=1.5)
    monkeypatch.setattr(
        shim, "estimate_timeout_close_economics", lambda **_kwargs: _economics(-1.20)
    )
    process_recovery_bot_after_normal_candle(
        tracker,
        sim,
        result=SimpleNamespace(),
        candle=_candle(datetime(2026, 1, 1, tzinfo=timezone.utc), 1.0),
        local_candle_index=11,
        absolute_candle_index=11,
        candle_fills=[],
        cumulative_pnl=0.0,
        trade_still_open=True,
    )
    process_recovery_bot_after_normal_candle(
        tracker,
        sim,
        result=SimpleNamespace(),
        candle=_candle(datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=5), 0.9),
        local_candle_index=12,
        absolute_candle_index=12,
        candle_fills=[],
        cumulative_pnl=0.0,
        trade_still_open=False,
    )
    assert sum(
        1
        for event in tracker.diagnostic_events
        if event["purpose"] == "RECOVERY_MAX_ADDITIONAL_LOSS_CLOSE_ALL"
    ) == 1


def test_cli_additional_loss_enables_recovery_without_recovery_bot_flag() -> None:
    from argparse import Namespace

    from research.backtests.run_original_hedge_backtest import resolve_recovery_bot_config

    args = Namespace(
        recovery_bot_config_json=None,
        no_recovery_bot=False,
        recovery_bot=False,
        recovery_start_purpose="CYCLE_4_LONG_ADD",
        recovery_wait_candles=576,
        recovery_timeout_action="close_all",
        recovery_timeout_min_loss_usdt=None,
        recovery_max_loss_usdt=1.50,
        recovery_max_additional_loss_usdt=0.70,
    )
    cfg = resolve_recovery_bot_config(args)
    assert cfg is not None
    assert cfg.enabled is True
    assert cfg.recovery_max_additional_loss_usdt == pytest.approx(0.70)
    assert cfg.recovery_max_loss_usdt == pytest.approx(1.50)

    args_only = Namespace(
        recovery_bot_config_json=None,
        no_recovery_bot=False,
        recovery_bot=False,
        recovery_start_purpose="CYCLE_4_LONG_ADD",
        recovery_wait_candles=576,
        recovery_timeout_action="gap_reduction",
        recovery_timeout_min_loss_usdt=None,
        recovery_max_loss_usdt=None,
        recovery_max_additional_loss_usdt=0.70,
    )
    cfg_only = resolve_recovery_bot_config(args_only)
    assert cfg_only is not None
    assert cfg_only.enabled is True
    assert cfg_only.recovery_max_additional_loss_usdt == pytest.approx(0.70)

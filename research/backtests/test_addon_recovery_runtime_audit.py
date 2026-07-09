from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fixed_cycle_hedge_bot.models import FillEvent

from research.backtests.addon_short_recovery import AddonShortRecoveryConfig
from research.backtests.addon_short_recovery_shim import (
    AddonShortRecoveryTracker,
    AddonShortRecoveryState,
    _maybe_activate_on_fills,
    _open_addon_short_immediately_at_activation_price,
    _maybe_close_addon_short_on_candle,
    _maybe_long_reduce_after_tp,
    process_addon_short_recovery_on_candle,
    record_addon_recovery_series_end,
)
from research.backtests.backtest_audit_recorder import BacktestAuditRecorder
from research.backtests.simulated_order_book import SimulatedOrderBook, SyntheticCandle


class _FakeSim:
    def __init__(self) -> None:
        self.symbol = "APTUSDT"
        self.candle_index = 0
        self.candle = SyntheticCandle(symbol=self.symbol, close=100.0, timestamp=datetime.now(timezone.utc))
        self.book = SimulatedOrderBook(symbol=self.symbol)
        self.audit_recorder: BacktestAuditRecorder | None = None
        # Minimal attributes used by shim helpers.
        RS = type("RS", (), {})
        self.runtime_state = RS()
        self.runtime_state.active_orders = {}
        self.runtime_state.exchange_to_client_id = {}
        self.runtime_state.client_to_exchange_id = {}
        # Minimal config stub so _record_addon_event can access trade_id safely.
        self.config = type("Cfg", (), {})()

    def _record_order_event(self, *args, **kwargs) -> None:  # pragma: no cover - shim stub
        return
        self.config = type("Cfg", (), {})()


def _activation_fill(purpose: str, price: float) -> FillEvent:
    return FillEvent(
        exchange_order_id="ex",
        client_order_id="cl",
        side="short",
        purpose=purpose,
        exec_qty=1.0,
        exec_price=price,
        order_type="Market",
        reduce_only=True,
        status="FILLED",
        exec_id="id",
        metadata={},
        occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_addon_activation_audit_record_created() -> None:
    sim = _FakeSim()
    recorder = BacktestAuditRecorder(enabled=True)
    sim.audit_recorder = recorder
    cfg = AddonShortRecoveryConfig(enabled=True)
    tracker = AddonShortRecoveryTracker(config=cfg, state=AddonShortRecoveryState())

    # Simulate activation fill matching activation_order.
    fill = _activation_fill(cfg.activation_order, 1.9)
    candle = SyntheticCandle(symbol=sim.symbol, close=1.9, timestamp=fill.occurred_at)
    _maybe_activate_on_fills(sim=sim, tracker=tracker, fills=[fill], candle=candle, candle_index=10)

    assert tracker.state.activated is True
    assert len(recorder.addon_events) == 1
    ev = recorder.addon_events[0]
    assert ev.event_type == "RECOVERY_ACTIVATED"
    assert ev.candle_index == 10
    assert ev.recovery_active_before is False
    assert ev.recovery_active_after is True


def test_long_reduce_audit_record_links_profit_and_reduce() -> None:
    sim = _FakeSim()
    recorder = BacktestAuditRecorder(enabled=True)
    sim.audit_recorder = recorder
    sim.book.audit_recorder = recorder
    cfg = AddonShortRecoveryConfig(enabled=True)
    state = AddonShortRecoveryState()
    state.activated = True
    state.addon_short_trade_count = 1
    tracker = AddonShortRecoveryTracker(config=cfg, state=state)

    # Set main book state before reduce.
    sim.book.long_qty = 10.0
    sim.book.long_avg = 100.0
    sim.book.short_qty = 5.0
    close_price = 95.0
    candle = SyntheticCandle(symbol=sim.symbol, close=close_price, timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc))

    short_trade_pnl = 5.0
    _maybe_long_reduce_after_tp(
        sim=sim,
        result=type("R", (), {"fill_log": []})(),  # minimal BacktestResult stub
        tracker=tracker,
        close_price=close_price,
        short_trade_pnl=short_trade_pnl,
        candle=candle,
        candle_index=20,
    )

    # Long-reduce should have been attempted and audited.
    assert tracker.state.long_reduce_total_qty > 0
    # There must be at least one addon audit event of type ADDON_LONG_REDUCE.
    long_reduce_events = [e for e in recorder.addon_events if e.event_type == "ADDON_LONG_REDUCE"]
    assert long_reduce_events, "expected at least one ADDON_LONG_REDUCE audit event"
    ev = long_reduce_events[0]
    assert ev.configured_profit_usage_fraction == pytest.approx(cfg.long_reduce_profit_usage_fraction)
    assert ev.short_profit_available == pytest.approx(short_trade_pnl)
    # usable profit should equal short_trade_pnl * fraction
    assert ev.short_profit_usable == pytest.approx(
        short_trade_pnl * cfg.long_reduce_profit_usage_fraction
    )
    assert ev.executed_reduce_qty is not None
    assert ev.long_avg_before_reduce == pytest.approx(100.0)
    # Fill linkage must point to the synthetic long-reduce fill.
    assert len(recorder.fills) == 1
    fill_rec = recorder.fills[0]
    assert ev.related_fill_order_id == fill_rec.order_id
    assert ev.related_fill_event_sequence == fill_rec.global_event_sequence
    assert ev.related_fill_event_sequence_in_candle == fill_rec.event_sequence_in_candle

    # Gap invariants: long-reduce must reduce remaining gap when it is positive.
    assert ev.long_qty_before == pytest.approx(10.0)
    assert ev.normal_short_qty_before == pytest.approx(5.0)
    assert ev.long_qty_after < ev.long_qty_before
    assert ev.normal_short_qty_after == pytest.approx(ev.normal_short_qty_before)
    assert ev.remaining_gap_before == pytest.approx(max(10.0 - 5.0, 0.0))
    assert ev.remaining_gap_after < ev.remaining_gap_before


def test_immediate_first_entry_audit_record_created() -> None:
    sim = _FakeSim()
    recorder = BacktestAuditRecorder(enabled=True)
    sim.audit_recorder = recorder
    cfg = AddonShortRecoveryConfig(enabled=True)
    state = AddonShortRecoveryState()
    state.activated = True
    state.activation_candle_index = 3
    state.activation_timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
    state.activation_price = 100.0
    state.addon_short_step_qty = 2.0
    tracker = AddonShortRecoveryTracker(config=cfg, state=state)

    # Main book state at activation.
    sim.book.long_qty = 10.0
    sim.book.short_qty = 4.0
    sim.candle = SyntheticCandle(symbol=sim.symbol, close=100.0, timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc))

    _open_addon_short_immediately_at_activation_price(sim=sim, tracker=tracker)

    events = [e for e in recorder.addon_events if e.event_type == "ADDON_SHORT_FIRST_ENTRY"]
    assert events, "expected ADDON_SHORT_FIRST_ENTRY audit event"
    ev = events[0]
    assert ev.first_entry_or_reentry == "first_entry"
    assert ev.requested_entry_qty == pytest.approx(2.0)
    assert ev.executed_entry_qty == pytest.approx(2.0)
    assert ev.addon_short_qty_after == pytest.approx(2.0)
    assert ev.remaining_gap_before_entry == pytest.approx(6.0)
    # Remaining gap after entry uses normal short + addon qty.
    assert ev.remaining_gap_after_entry == pytest.approx(10.0 - (4.0 + 2.0))


def test_reentry_audit_record_created() -> None:
    sim = _FakeSim()
    recorder = BacktestAuditRecorder(enabled=True)
    sim.audit_recorder = recorder
    sim.book.audit_recorder = recorder
    cfg = AddonShortRecoveryConfig(enabled=True)
    state = AddonShortRecoveryState()
    state.activated = True
    # Simulate one completed trade already.
    state.addon_short_trade_count = 1
    tracker = AddonShortRecoveryTracker(config=cfg, state=state)

    # Book and candle where reentry trigger should fire.
    sim.book.long_qty = 10.0
    sim.book.short_qty = 4.0
    state.previous_low = 100.0
    state.addon_short_step_qty = 2.0
    candle = SyntheticCandle(symbol=sim.symbol, close=99.0, low=99.0, high=101.0, timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc))

    # No existing addon short, so this should open a new trade via distance logic.
    result_stub = type("R", (), {"fill_log": []})()
    process_addon_short_recovery_on_candle(
        sim=sim,
        result=result_stub,
        tracker=tracker,
        candle=candle,
        candle_index=5,
        candle_fills=[],
    )

    events = [e for e in recorder.addon_events if e.event_type == "ADDON_SHORT_REENTRY"]
    assert events, "expected ADDON_SHORT_REENTRY audit event"
    ev = events[0]
    assert ev.first_entry_or_reentry == "reentry"
    assert ev.requested_entry_qty == pytest.approx(ev.executed_entry_qty)
    assert ev.entry_price is not None
    assert ev.entry_trigger_price is not None
    assert ev.entry_reference_low == pytest.approx(100.0)
    assert ev.reentry_buffer_pct == pytest.approx(cfg.addon_short_reentry_buffer_pct)


def test_tp_close_audit_record_includes_quantities_and_pnl() -> None:
    sim = _FakeSim()
    recorder = BacktestAuditRecorder(enabled=True)
    sim.audit_recorder = recorder
    cfg = AddonShortRecoveryConfig(enabled=True)
    state = AddonShortRecoveryState()
    state.activated = True
    state.has_open_addon_short = True
    state.addon_short_entry_price = 100.0
    state.addon_short_qty_open = 2.0
    state.addon_short_trade_count = 1
    tracker = AddonShortRecoveryTracker(config=cfg, state=state)

    sim.book.fee_rate = 0.0
    candle = SyntheticCandle(
        symbol=sim.symbol,
        open=100.0,
        high=100.5,
        low=99.0,  # below TP threshold
        close=99.5,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    closed, close_price, reason = _maybe_close_addon_short_on_candle(
        sim=sim,
        tracker=tracker,
        candle=candle,
        candle_index=7,
        previous_low_for_trailing=state.previous_low,
    )

    assert closed is True
    assert reason == "tp"
    assert close_price is not None

    events = [e for e in recorder.addon_events if e.event_type == "ADDON_SHORT_TP_CLOSE"]
    assert events, "expected ADDON_SHORT_TP_CLOSE audit event"
    ev = events[0]
    assert ev.requested_close_qty == pytest.approx(2.0)
    assert ev.executed_close_qty == pytest.approx(2.0)
    assert ev.close_price == pytest.approx(close_price)
    assert ev.close_reason == "tp"
    assert ev.tp_price is not None
    assert ev.gross_pnl == pytest.approx(ev.net_pnl)
    assert ev.addon_trade_id == 1


def test_rebound_close_audit_record_includes_quantities_and_pnl() -> None:
    sim = _FakeSim()
    recorder = BacktestAuditRecorder(enabled=True)
    sim.audit_recorder = recorder
    cfg = AddonShortRecoveryConfig(enabled=True)
    state = AddonShortRecoveryState()
    state.activated = True
    state.has_open_addon_short = True
    state.addon_short_entry_price = 100.0
    state.addon_short_qty_open = 1.0
    state.addon_short_trade_count = 1
    # Trailing low deep enough to allow rebound.
    trailing_low = 98.0
    tracker = AddonShortRecoveryTracker(config=cfg, state=state)

    sim.book.fee_rate = 0.0
    candle = SyntheticCandle(
        symbol=sim.symbol,
        open=99.0,
        high=99.5,  # rebound close should be within reach
        low=99.3,  # stay above TP threshold to avoid TP
        close=99.0,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    closed, close_price, reason = _maybe_close_addon_short_on_candle(
        sim=sim,
        tracker=tracker,
        candle=candle,
        candle_index=8,
        previous_low_for_trailing=trailing_low,
    )

    assert closed is True
    assert reason == "rebound"
    assert close_price is not None

    events = [e for e in recorder.addon_events if e.event_type == "ADDON_SHORT_REBOUND_CLOSE"]
    assert events, "expected ADDON_SHORT_REBOUND_CLOSE audit event"
    ev = events[0]
    assert ev.requested_close_qty == pytest.approx(1.0)
    assert ev.executed_close_qty == pytest.approx(1.0)
    assert ev.close_price == pytest.approx(close_price)
    assert ev.close_reason == "rebound"
    assert ev.rebound_price == pytest.approx(close_price)
    assert ev.gross_pnl == pytest.approx(ev.net_pnl)


def test_hard_stop_close_audit_record_includes_quantities_and_pnl() -> None:
    sim = _FakeSim()
    recorder = BacktestAuditRecorder(enabled=True)
    sim.audit_recorder = recorder
    cfg = AddonShortRecoveryConfig(enabled=True)
    state = AddonShortRecoveryState()
    state.activated = True
    state.has_open_addon_short = True
    state.addon_short_entry_price = 100.0
    state.addon_short_qty_open = 1.0
    state.addon_short_trade_count = 1
    tracker = AddonShortRecoveryTracker(config=cfg, state=state)

    sim.book.fee_rate = 0.0
    # High above hard-stop threshold.
    candle = SyntheticCandle(
        symbol=sim.symbol,
        open=100.0,
        high=102.0,
        low=99.5,
        close=101.0,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    closed, close_price, reason = _maybe_close_addon_short_on_candle(
        sim=sim,
        tracker=tracker,
        candle=candle,
        candle_index=9,
        previous_low_for_trailing=state.previous_low,
    )

    assert closed is True
    assert reason == "hard_stop"
    assert close_price is not None

    events = [e for e in recorder.addon_events if e.event_type == "ADDON_SHORT_HARD_STOP_CLOSE"]
    assert events, "expected ADDON_SHORT_HARD_STOP_CLOSE audit event"
    ev = events[0]
    assert ev.requested_close_qty == pytest.approx(1.0)
    assert ev.executed_close_qty == pytest.approx(1.0)
    assert ev.close_price == pytest.approx(close_price)
    assert ev.close_reason == "hard_stop"
    assert ev.hard_stop_price == pytest.approx(close_price)
    assert ev.gross_pnl == pytest.approx(ev.net_pnl)


def test_recovery_completion_event_logged_when_long_leq_short() -> None:
    sim = _FakeSim()
    recorder = BacktestAuditRecorder(enabled=True)
    sim.audit_recorder = recorder
    cfg = AddonShortRecoveryConfig(enabled=True)
    state = AddonShortRecoveryState()
    state.activated = True
    tracker = AddonShortRecoveryTracker(config=cfg, state=state)

    # No addon short open and long qty <= normal short qty.
    sim.book.long_qty = 4.0
    sim.book.short_qty = 5.0
    candle = SyntheticCandle(symbol=sim.symbol, close=100.0, timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc))

    result_stub = type("R", (), {"fill_log": []})()
    process_addon_short_recovery_on_candle(
        sim=sim,
        result=result_stub,
        tracker=tracker,
        candle=candle,
        candle_index=11,
        candle_fills=[],
    )

    events = [e for e in recorder.addon_events if e.event_type == "RECOVERY_COMPLETED"]
    assert events, "expected RECOVERY_COMPLETED audit event"
    ev = events[0]
    assert ev.recovery_completed_before is False
    assert ev.recovery_completed_after is True
    assert ev.recovery_completion_candle_index == 11
    assert ev.combined_short_qty_before == pytest.approx(ev.normal_short_qty_before)
    assert ev.remaining_gap_before == pytest.approx(0.0)


def test_series_end_event_logs_final_state() -> None:
    sim = _FakeSim()
    recorder = BacktestAuditRecorder(enabled=True)
    sim.audit_recorder = recorder
    sim.book.audit_recorder = recorder
    cfg = AddonShortRecoveryConfig(enabled=True)
    state = AddonShortRecoveryState()
    state.activated = True
    state.addon_short_trade_count = 2
    state.addon_short_realized_profit = 5.0
    state.addon_short_realized_loss = 1.0
    state.long_reduce_total_qty = 3.0
    state.long_reduce_total_pnl = 2.5
    tracker = AddonShortRecoveryTracker(config=cfg, state=state)

    sim.book.long_qty = 10.0
    sim.book.short_qty = 4.0
    last_candle = SyntheticCandle(symbol=sim.symbol, close=101.0, timestamp=datetime(2026, 1, 2, tzinfo=timezone.utc))
    result_stub = type("R", (), {})()

    record_addon_recovery_series_end(
        sim=sim,
        tracker=tracker,
        result=result_stub,
        last_candle=last_candle,
        last_candle_index=15,
    )

    events = [e for e in recorder.addon_events if e.event_type == "RECOVERY_SERIES_END"]
    assert events, "expected RECOVERY_SERIES_END audit event"
    ev = events[0]
    assert ev.candle_index == 15
    assert ev.close_price == pytest.approx(101.0)
    assert ev.addon_short_realized_profit_after == pytest.approx(5.0)
    assert ev.addon_short_realized_loss_after == pytest.approx(1.0)
    assert ev.long_reduce_total_qty_after == pytest.approx(3.0)
    assert ev.long_reduce_total_pnl_after == pytest.approx(2.5)


def test_joint_sequence_tp_long_reduce_and_fill_in_same_candle() -> None:
    sim = _FakeSim()
    recorder = BacktestAuditRecorder(enabled=True)
    sim.audit_recorder = recorder
    sim.book.audit_recorder = recorder
    cfg = AddonShortRecoveryConfig(enabled=True)
    state = AddonShortRecoveryState()
    state.activated = True
    state.has_open_addon_short = True
    state.addon_short_entry_price = 100.0
    state.addon_short_qty_open = 2.0
    state.addon_short_trade_count = 1
    tracker = AddonShortRecoveryTracker(config=cfg, state=state)

    sim.book.fee_rate = 0.0
    sim.book.long_qty = 10.0
    sim.book.long_avg = 100.0
    sim.book.short_qty = 4.0
    sim.candle_index = 12
    sim.book.current_candle_index = 12
    candle = SyntheticCandle(
        symbol=sim.symbol,
        open=100.0,
        high=100.5,
        low=99.0,
        close=99.5,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    # First close via TP.
    closed, close_price, reason = _maybe_close_addon_short_on_candle(
        sim=sim,
        tracker=tracker,
        candle=candle,
        candle_index=12,
        previous_low_for_trailing=state.previous_low,
    )
    assert closed and reason == "tp" and close_price is not None

    # Then long-reduce based on that TP profit.
    last_ev = tracker.events[-1]
    short_pnl = float(last_ev.net_pnl or 0.0)
    _maybe_long_reduce_after_tp(
        sim=sim,
        result=type("R", (), {"fill_log": []})(),
        tracker=tracker,
        close_price=close_price,
        short_trade_pnl=short_pnl,
        candle=candle,
        candle_index=12,
    )

    # Merge addon and fill records and assert sequencing properties.
    all_records = list(recorder.addon_events) + list(recorder.fills)
    # Filter down to this candle/index.
    all_records = [r for r in all_records if r.candle_index == 12]
    assert all_records, "expected events and fills for candle_index=12"
    all_records.sort(key=lambda r: r.global_event_sequence)

    seqs = [r.global_event_sequence for r in all_records]
    assert len(seqs) == len(set(seqs)), "global_event_sequence must be unique"
    assert seqs == sorted(seqs), "global_event_sequence must be strictly increasing"

    # Event types and order should reflect actual runtime:
    # 1) TP close addon event, 2) long-reduce fill, 3) long-reduce addon event.
    first = next(r for r in all_records if getattr(r, "event_type", "") == "ADDON_SHORT_TP_CLOSE")
    last = next(r for r in all_records if getattr(r, "event_type", "") == "ADDON_LONG_REDUCE")
    fill = next(r for r in all_records if getattr(r, "event_type", "") == "fill")

    assert first.global_event_sequence < fill.global_event_sequence < last.global_event_sequence
    assert first.candle_index == fill.candle_index == last.candle_index == 12
    assert last.associated_addon_close_event_sequence == first.global_event_sequence
    assert last.associated_addon_trade_id == first.addon_trade_id


def test_recorder_disabled_produces_no_addon_audit_records() -> None:
    sim = _FakeSim()
    recorder = BacktestAuditRecorder(enabled=False)
    sim.audit_recorder = recorder
    cfg = AddonShortRecoveryConfig(enabled=True)
    state = AddonShortRecoveryState()
    state.activated = True
    state.has_open_addon_short = True
    state.addon_short_entry_price = 100.0
    state.addon_short_qty_open = 1.0
    state.addon_short_trade_count = 1
    tracker = AddonShortRecoveryTracker(config=cfg, state=state)

    candle = SyntheticCandle(
        symbol=sim.symbol,
        open=100.0,
        high=102.0,
        low=99.0,
        close=101.0,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    _maybe_close_addon_short_on_candle(
        sim=sim,
        tracker=tracker,
        candle=candle,
        candle_index=13,
        previous_low_for_trailing=state.previous_low,
    )

    assert recorder.addon_events == []




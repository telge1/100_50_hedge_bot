from __future__ import annotations

from pathlib import Path

import pytest

from research.backtests.backtest_audit_recorder import BacktestAuditRecorder
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.simulated_order_book import SimulatedOrderBook
from research.backtests.simulated_order_book import SyntheticCandle
from fixed_cycle_hedge_bot.models import StrategyIntent


def test_backtest_audit_recorder_sequences() -> None:
    recorder = BacktestAuditRecorder(enabled=True)

    seq1 = recorder.next_event_sequence(candle_index=0)
    seq2 = recorder.next_event_sequence(candle_index=0)
    seq3 = recorder.next_event_sequence(candle_index=1)

    assert seq1 == (1, 1)
    assert seq2 == (2, 2)
    assert seq3 == (3, 1)


def _simple_book_with_long_position() -> SimulatedOrderBook:
    book = SimulatedOrderBook(symbol="TESTUSDT")
    # Open long position: qty=10 @ 100
    book.long_qty = 10.0
    book.long_avg = 100.0
    return book


def _simple_book_with_short_position() -> SimulatedOrderBook:
    book = SimulatedOrderBook(symbol="TESTUSDT")
    # Open short position: qty=10 @ 100
    book.short_qty = 10.0
    book.short_avg = 100.0
    return book


def test_apply_fill_long_partial_reduce_audit_record() -> None:
    recorder = BacktestAuditRecorder(enabled=True)
    book = _simple_book_with_long_position()
    book.audit_recorder = recorder
    book.current_candle_index = 5

    intent = StrategyIntent(
        side="long",
        qty=4.0,
        purpose="LONG_TP_EXIT",
        order_type="Market",
        reduce_only=True,
        trigger_price=None,
        price=None,
    )
    order, _ = book.submit_intent(intent, replace=False)

    order_filled, realized = book.apply_fill(order_id=order.order_id, fill_price=110.0, qty=4.0)

    assert pytest.approx(realized) == (110.0 - 100.0) * 4.0
    assert len(recorder.fills) == 1
    rec = recorder.fills[0]

    # Pre-state
    assert rec.long_qty_before == pytest.approx(10.0)
    assert rec.long_avg_before == pytest.approx(100.0)
    # Post-state (6 left, avg unchanged)
    assert rec.long_qty_after == pytest.approx(6.0)
    assert rec.long_avg_after == pytest.approx(100.0)
    # Executed qty must equal 4.0
    assert rec.executed_qty == pytest.approx(4.0)
    # Gross PnL should be 40
    assert rec.gross_pnl == pytest.approx(40.0)
    assert rec.closed_pnl == pytest.approx(40.0)


def test_apply_fill_long_open_fill_audit_record() -> None:
    recorder = BacktestAuditRecorder(enabled=True)
    book = SimulatedOrderBook(symbol="TESTUSDT")
    book.audit_recorder = recorder
    book.current_candle_index = 1

    intent = StrategyIntent(
        side="long",
        qty=5.0,
        purpose="INITIAL_LONG_ENTRY",
        order_type="Market",
        reduce_only=False,
        trigger_price=None,
        price=None,
    )
    order, _ = book.submit_intent(intent, replace=False)

    _order_filled, realized = book.apply_fill(order_id=order.order_id, fill_price=100.0, qty=5.0)

    assert realized == pytest.approx(0.0)
    rec = recorder.fills[0]
    assert rec.long_qty_before == pytest.approx(0.0)
    assert rec.long_avg_before == pytest.approx(0.0)
    assert rec.long_qty_after == pytest.approx(5.0)
    assert rec.long_avg_after == pytest.approx(100.0)
    assert rec.closed_pnl == pytest.approx(0.0)


def test_apply_fill_long_full_reduce_audit_record() -> None:
    recorder = BacktestAuditRecorder(enabled=True)
    book = _simple_book_with_long_position()
    book.audit_recorder = recorder
    book.current_candle_index = 2

    intent = StrategyIntent(
        side="long",
        qty=10.0,
        purpose="LONG_TP_EXIT",
        order_type="Market",
        reduce_only=True,
        trigger_price=None,
        price=None,
    )
    order, _ = book.submit_intent(intent, replace=False)

    _order_filled, realized = book.apply_fill(order_id=order.order_id, fill_price=110.0, qty=10.0)

    assert realized == pytest.approx((110.0 - 100.0) * 10.0)
    rec = recorder.fills[0]
    assert rec.long_qty_before == pytest.approx(10.0)
    assert rec.long_qty_after == pytest.approx(0.0)
    assert rec.long_avg_after == pytest.approx(0.0)


def test_apply_fill_short_open_fill_audit_record() -> None:
    recorder = BacktestAuditRecorder(enabled=True)
    book = SimulatedOrderBook(symbol="TESTUSDT")
    book.audit_recorder = recorder
    book.current_candle_index = 3

    intent = StrategyIntent(
        side="short",
        qty=5.0,
        purpose="INITIAL_SHORT_ENTRY",
        order_type="Market",
        reduce_only=False,
        trigger_price=None,
        price=None,
    )
    order, _ = book.submit_intent(intent, replace=False)

    _order_filled, realized = book.apply_fill(order_id=order.order_id, fill_price=100.0, qty=5.0)

    assert realized == pytest.approx(0.0)
    rec = recorder.fills[0]
    assert rec.short_qty_before == pytest.approx(0.0)
    assert rec.short_avg_before == pytest.approx(0.0)
    assert rec.short_qty_after == pytest.approx(5.0)
    assert rec.short_avg_after == pytest.approx(100.0)
    assert rec.closed_pnl == pytest.approx(0.0)


def test_apply_fill_short_partial_reduce_audit_record() -> None:
    recorder = BacktestAuditRecorder(enabled=True)
    book = _simple_book_with_short_position()
    book.audit_recorder = recorder
    book.current_candle_index = 4

    intent = StrategyIntent(
        side="short",
        qty=4.0,
        purpose="SHORT_TP_EXIT",
        order_type="Market",
        reduce_only=True,
        trigger_price=None,
        price=None,
    )
    order, _ = book.submit_intent(intent, replace=False)

    _order_filled, realized = book.apply_fill(order_id=order.order_id, fill_price=90.0, qty=4.0)

    # Short profit: (entry - close) * qty = (100-90)*4
    assert realized == pytest.approx((100.0 - 90.0) * 4.0)
    rec = recorder.fills[0]
    assert rec.short_qty_before == pytest.approx(10.0)
    assert rec.short_avg_before == pytest.approx(100.0)
    assert rec.short_qty_after == pytest.approx(6.0)
    assert rec.short_avg_after == pytest.approx(100.0)
    assert rec.closed_pnl == pytest.approx((100.0 - 90.0) * 4.0)


def test_apply_fill_short_full_reduce_audit_record() -> None:
    recorder = BacktestAuditRecorder(enabled=True)
    book = _simple_book_with_short_position()
    book.audit_recorder = recorder
    book.current_candle_index = 5

    intent = StrategyIntent(
        side="short",
        qty=10.0,
        purpose="SHORT_TP_EXIT",
        order_type="Market",
        reduce_only=True,
        trigger_price=None,
        price=None,
    )
    order, _ = book.submit_intent(intent, replace=False)

    _order_filled, realized = book.apply_fill(order_id=order.order_id, fill_price=90.0, qty=10.0)

    assert realized == pytest.approx((100.0 - 90.0) * 10.0)
    rec = recorder.fills[0]
    assert rec.short_qty_before == pytest.approx(10.0)
    assert rec.short_qty_after == pytest.approx(0.0)
    assert rec.short_avg_after == pytest.approx(0.0)


def test_apply_fill_reduce_qty_larger_than_position_clamped() -> None:
    recorder = BacktestAuditRecorder(enabled=True)
    book = _simple_book_with_long_position()
    book.audit_recorder = recorder
    book.current_candle_index = 6

    # Request to reduce more than available (qty=15 > long_qty=10).
    intent = StrategyIntent(
        side="long",
        qty=15.0,
        purpose="LONG_TP_EXIT",
        order_type="Market",
        reduce_only=True,
        trigger_price=None,
        price=None,
    )
    order, _ = book.submit_intent(intent, replace=False)

    _order_filled, realized = book.apply_fill(order_id=order.order_id, fill_price=110.0, qty=15.0)

    # Only 10 can actually be closed.
    assert realized == pytest.approx((110.0 - 100.0) * 10.0)
    rec = recorder.fills[0]
    assert rec.requested_qty == pytest.approx(15.0)
    # executed_qty is the actual close_qty used for PnL (10.0)
    assert rec.executed_qty == pytest.approx(10.0)
    assert rec.long_qty_before == pytest.approx(10.0)
    assert rec.long_qty_after == pytest.approx(0.0)



def test_audit_disabled_has_no_records() -> None:
    recorder = BacktestAuditRecorder(enabled=False)
    book = _simple_book_with_long_position()
    book.audit_recorder = recorder
    book.current_candle_index = 0

    intent = StrategyIntent(
        side="long",
        qty=2.0,
        purpose="LONG_TP_EXIT",
        order_type="Market",
        reduce_only=True,
        trigger_price=None,
        price=None,
    )
    order, _ = book.submit_intent(intent, replace=False)

    _order_filled, _realized = book.apply_fill(order_id=order.order_id, fill_price=105.0, qty=2.0)

    assert recorder.fills == []


def _simple_candles(prices: list[float]) -> list[dict]:
    return [
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "open": p,
            "high": p,
            "low": p,
            "close": p,
            "volume": 0.0,
        }
        for p in prices
    ]


def test_identity_run_historical_backtest_with_and_without_audit(tmp_path: Path) -> None:
    candles = _simple_candles([100.0, 101.0, 102.0, 103.0])

    baseline = run_historical_backtest(
        symbol="APTUSDT",
        direction="long",
        candles=candles,
        max_candles=2,
    )

    recorder = BacktestAuditRecorder(enabled=True)
    with_audit = run_historical_backtest(
        symbol="APTUSDT",
        direction="long",
        candles=candles,
        max_candles=2,
        audit_recorder=recorder,
    )

    # BacktestResult: core numerical and structural fields must match.
    assert baseline.realized_pnl == pytest.approx(with_audit.realized_pnl)
    assert baseline.unrealized_pnl == pytest.approx(with_audit.unrealized_pnl)
    assert baseline.overall_pnl == pytest.approx(with_audit.overall_pnl)
    assert baseline.final_long_qty == pytest.approx(with_audit.final_long_qty)
    assert baseline.final_short_qty == pytest.approx(with_audit.final_short_qty)
    assert baseline.fills_count == with_audit.fills_count
    assert baseline.orders_submitted == with_audit.orders_submitted

    # Fill timeline: same length and identical key fields for each fill.
    assert len(baseline.fill_log) == len(with_audit.fill_log)
    for base_entry, audit_entry in zip(baseline.fill_log, with_audit.fill_log):
        for key in (
            "order_id",
            "purpose",
            "side",
            "qty",
            "fill_price",
            "closed_pnl",
            "candle_index",
            "long_qty_after",
            "short_qty_after",
            "long_avg_after",
            "short_avg_after",
        ):
            assert base_entry.get(key) == audit_entry.get(key)

    # Order timeline: ensure order count and core fields are unchanged.
    assert len(baseline.order_log) == len(with_audit.order_log)
    for base_order, audit_order in zip(baseline.order_log, with_audit.order_log):
        for key in ("order_id", "side", "qty", "price", "trigger_price", "status"):
            assert base_order.get(key) == audit_order.get(key)



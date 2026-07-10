"""Minimal deterministic PnL reconciliation tests (A–E)."""

from __future__ import annotations

import pytest

from fixed_cycle_hedge_bot.fixed_cycle_strategy import (
    FixedCycleHedgeStrategy,
    ShortFixedCycleHedgeStrategy,
)
from fixed_cycle_hedge_bot.math_utils import calculate_pnl
from fixed_cycle_hedge_bot.models import RuntimeState, StrategyIntent

from research.backtests.simulated_execution import (
    fill_order_at_price,
    resolve_simulated_fee_rate,
)
from research.backtests.simulated_order_book import SimulatedOrderBook
from research.backtests.simulated_pnl import calculate_simulated_closed_pnl

FEE_RATE = resolve_simulated_fee_rate()


def _submit_reduce(
    book: SimulatedOrderBook,
    *,
    side: str,
    qty: float,
    purpose: str,
) -> str:
    order, _ = book.submit_intent(
        StrategyIntent(
            side=side,
            qty=qty,
            purpose=purpose,
            order_type="Market",
            reduce_only=True,
        ),
        replace=False,
    )
    return order.order_id


def _submit_open(
    book: SimulatedOrderBook,
    *,
    side: str,
    qty: float,
    purpose: str,
    price: float,
) -> str:
    order, _ = book.submit_intent(
        StrategyIntent(
            side=side,
            qty=qty,
            purpose=purpose,
            order_type="Market",
            reduce_only=False,
            price=price,
        ),
        replace=False,
    )
    return order.order_id


def test_a_simple_long_reduce_net_pnl() -> None:
    avg = 1.676
    exit_price = 1.6899
    qty = 59.665
    gross = calculate_pnl(avg, exit_price, qty, "long")
    net, details = calculate_simulated_closed_pnl(
        side="long",
        avg_entry_price=avg,
        fill_price=exit_price,
        qty=qty,
        reduce_only=True,
        fee_rate=FEE_RATE,
    )
    assert gross == pytest.approx(0.8293, abs=1e-4)
    assert net == pytest.approx(0.7189, abs=1e-4)
    assert details["entry_fee"] == pytest.approx(abs(avg * qty) * FEE_RATE, rel=1e-6)
    assert details["exit_fee"] == pytest.approx(abs(exit_price * qty) * FEE_RATE, rel=1e-6)


def test_b_simple_short_reduce_net_pnl() -> None:
    avg = 1.676
    exit_price = 1.6899
    qty = 29.832
    gross = calculate_pnl(avg, exit_price, qty, "short")
    net, _ = calculate_simulated_closed_pnl(
        side="short",
        avg_entry_price=avg,
        fill_price=exit_price,
        qty=qty,
        reduce_only=True,
        fee_rate=FEE_RATE,
    )
    assert gross == pytest.approx(-0.4147, abs=1e-4)
    assert net == pytest.approx(-0.4699, abs=1e-4)


def test_c_paired_exit_same_candle_matches_book() -> None:
    book = SimulatedOrderBook(symbol="APTUSDT", fee_rate=FEE_RATE)
    runtime = RuntimeState(strategy_state={})
    avg = 1.676
    exit_price = 1.6899
    long_qty = 59.665
    short_qty = 29.832

    book.apply_fill(
        order_id=_submit_open(
            book, side="long", qty=long_qty, purpose="INITIAL_LONG_ENTRY", price=avg
        ),
        fill_price=avg,
    )
    book.apply_fill(
        order_id=_submit_open(
            book, side="short", qty=short_qty, purpose="INITIAL_SHORT_ENTRY", price=avg
        ),
        fill_price=avg,
    )

    long_order = _submit_reduce(book, side="long", qty=long_qty, purpose="LONG_TP_EXIT")
    short_order = _submit_reduce(book, side="short", qty=short_qty, purpose="SHORT_SL_EXIT")

    long_fill = fill_order_at_price(
        book=book, runtime_state=runtime, order_id=long_order, fill_price=exit_price
    )
    short_fill = fill_order_at_price(
        book=book, runtime_state=runtime, order_id=short_order, fill_price=exit_price
    )

    long_net = float(long_fill.metadata.get("closed_pnl") or 0.0)
    short_net = float(short_fill.metadata.get("closed_pnl") or 0.0)
    assert long_net == pytest.approx(0.7189, abs=1e-4)
    assert short_net == pytest.approx(-0.4699, abs=1e-4)
    assert long_net + short_net == pytest.approx(0.2490, abs=1e-4)
    assert book.long_qty == pytest.approx(0.0)
    assert book.short_qty == pytest.approx(0.0)


def test_d_unequal_long_short_qty_no_spread_shortcut() -> None:
    book = SimulatedOrderBook(symbol="APTUSDT", fee_rate=FEE_RATE)
    runtime = RuntimeState(strategy_state={})
    long_avg = 2.0
    short_avg = 2.1
    long_qty = 40.0
    short_qty = 17.5
    long_exit = 2.05
    short_exit = 2.08

    book.apply_fill(
        order_id=_submit_open(
            book, side="long", qty=long_qty, purpose="INITIAL_LONG_ENTRY", price=long_avg
        ),
        fill_price=long_avg,
    )
    book.apply_fill(
        order_id=_submit_open(
            book, side="short", qty=short_qty, purpose="INITIAL_SHORT_ENTRY", price=short_avg
        ),
        fill_price=short_avg,
    )

    expected_long, _ = calculate_simulated_closed_pnl(
        side="long",
        avg_entry_price=long_avg,
        fill_price=long_exit,
        qty=long_qty,
        reduce_only=True,
        fee_rate=FEE_RATE,
    )
    expected_short, _ = calculate_simulated_closed_pnl(
        side="short",
        avg_entry_price=short_avg,
        fill_price=short_exit,
        qty=short_qty,
        reduce_only=True,
        fee_rate=FEE_RATE,
    )

    long_fill = fill_order_at_price(
        book=book,
        runtime_state=runtime,
        order_id=_submit_reduce(book, side="long", qty=long_qty, purpose="LONG_TP_EXIT"),
        fill_price=long_exit,
    )
    short_fill = fill_order_at_price(
        book=book,
        runtime_state=runtime,
        order_id=_submit_reduce(book, side="short", qty=short_qty, purpose="SHORT_SL_EXIT"),
        fill_price=short_exit,
    )

    assert float(long_fill.metadata["closed_pnl"]) == pytest.approx(expected_long, abs=1e-8)
    assert float(short_fill.metadata["closed_pnl"]) == pytest.approx(expected_short, abs=1e-8)
    spread_gross = (long_exit - long_avg) * min(long_qty, short_qty) + (
        short_avg - short_exit
    ) * min(long_qty, short_qty)
    assert expected_long + expected_short != pytest.approx(spread_gross, abs=1e-6)


@pytest.mark.parametrize("cycle_index", [1, 2, 3, 4])
def test_e_long_bot_cycle_purpose_reduce_only_mapping(cycle_index: int) -> None:
    strategy = FixedCycleHedgeStrategy()
    first_purpose = strategy._get_first_leg_purpose(cycle_index)
    second_purpose = strategy._get_second_leg_purpose(cycle_index)
    assert first_purpose == f"CYCLE_{cycle_index}_LONG_ADD"
    assert second_purpose == f"CYCLE_{cycle_index}_SHORT_REDUCE"
    assert strategy._get_first_leg_side() == "long"
    assert strategy._get_first_leg_cycle_role() == "long_reduce"
    assert strategy._get_second_leg_cycle_role() == "short_reduce"


@pytest.mark.parametrize("cycle_index", [1, 2, 3, 4])
def test_e_short_bot_cycle_purpose_reduce_only_mapping(cycle_index: int) -> None:
    strategy = ShortFixedCycleHedgeStrategy()
    first_purpose = strategy._get_first_leg_purpose(cycle_index)
    second_purpose = strategy._get_second_leg_purpose(cycle_index)
    assert first_purpose == f"CYCLE_{cycle_index}_SHORT_REDUCE"
    assert second_purpose == f"CYCLE_{cycle_index}_LONG_REDUCE"
    assert strategy._get_first_leg_side() == "short"
    assert strategy._get_first_leg_cycle_role() == "short_reduce"
    assert strategy._get_second_leg_cycle_role() == "long_reduce"

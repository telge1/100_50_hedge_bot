"""Integration tests for direction-neutral primary profit basis in TP projection."""

from __future__ import annotations

from unittest import mock

import pytest

from fixed_cycle_hedge_bot.fixed_cycle_strategy import (
    FixedCycleHedgeConfig,
    FixedCycleHedgeStrategy,
    ShortFixedCycleHedgeStrategy,
)
from fixed_cycle_hedge_bot.models import HedgeSnapshot, RuntimeState, snapshot_from_mapping


def _runtime() -> RuntimeState:
    state = RuntimeState(strategy_state={})
    state.instrument_rules["APTUSDT"] = {
        "tick_size": __import__("decimal").Decimal("0.0001"),
        "qty_step": __import__("decimal").Decimal("0.001"),
        "min_order_qty": __import__("decimal").Decimal("0.001"),
        "min_notional": __import__("decimal").Decimal("5"),
    }
    return state


def _snapshot(*, long_qty: float, short_qty: float, price: float = 2.0) -> HedgeSnapshot:
    runtime = _runtime()
    return snapshot_from_mapping(
        symbol="APTUSDT",
        current_price=price,
        positions={
            "long_qty": long_qty,
            "short_qty": short_qty,
            "long_avg": price,
            "short_avg": price,
        },
        runtime_state=runtime,
        source="test",
    )


def test_fixed_cycle_strategy_uses_long_primary_basis() -> None:
    strategy = FixedCycleHedgeStrategy(
        FixedCycleHedgeConfig(symbol="APTUSDT", tp_profit_target_pct=0.25, tp_buffer_pct=0.0002)
    )
    runtime = _runtime()
    snapshot = _snapshot(long_qty=50.0, short_qty=25.0)
    with mock.patch(
        "fixed_cycle_hedge_bot.fixed_cycle_strategy.calculate_hedge_exit_price",
        autospec=True,
    ) as calc:
        from fixed_cycle_hedge_bot import hedge_exit_math

        calc.side_effect = hedge_exit_math.calculate_hedge_exit_price
        strategy._calculate_tp_projection(2.0, snapshot, runtime)
        _, kwargs = calc.call_args
        assert kwargs["primary_side"] == "long"
        assert kwargs["long_qty"] == pytest.approx(50.0)
        assert kwargs["short_qty"] == pytest.approx(25.0)


def test_short_fixed_cycle_strategy_uses_short_primary_basis() -> None:
    strategy = ShortFixedCycleHedgeStrategy(
        FixedCycleHedgeConfig(
            symbol="APTUSDT",
            strategy_side="short",
            tp_profit_target_pct=0.25,
            tp_buffer_pct=0.0002,
            base_notional_usdt=50.0,
            hedge_ratio_short=2.0,
        )
    )
    runtime = _runtime()
    snapshot = _snapshot(long_qty=25.0, short_qty=50.0)
    with mock.patch(
        "fixed_cycle_hedge_bot.fixed_cycle_strategy.calculate_hedge_exit_price",
        autospec=True,
    ) as calc:
        from fixed_cycle_hedge_bot import hedge_exit_math

        calc.side_effect = hedge_exit_math.calculate_hedge_exit_price
        strategy._calculate_tp_projection(2.0, snapshot, runtime)
        _, kwargs = calc.call_args
        assert kwargs["primary_side"] == "short"
        assert kwargs["long_qty"] == pytest.approx(25.0)
        assert kwargs["short_qty"] == pytest.approx(50.0)


def test_short_strategy_tp_projection_profit_basis_is_short_notional() -> None:
    strategy = ShortFixedCycleHedgeStrategy(
        FixedCycleHedgeConfig(
            symbol="APTUSDT",
            strategy_side="short",
            tp_profit_target_pct=0.25,
            tp_buffer_pct=0.0002,
            base_notional_usdt=50.0,
            hedge_ratio_short=2.0,
        )
    )
    runtime = _runtime()
    snapshot = _snapshot(long_qty=25.0, short_qty=50.0, price=2.0)
    projection = strategy._calculate_tp_projection(2.0, snapshot, runtime)
    assert projection.components.profit_basis_usdt == pytest.approx(100.0)
    assert projection.components.target_profit_usdt == pytest.approx(0.25)
    assert projection.min_profit_target_usdt > 0.25


def test_short_strategy_exit_purposes_remain_mirrored() -> None:
    strategy = ShortFixedCycleHedgeStrategy()
    assert strategy._get_final_long_exit_purpose() == "LONG_SL_EXIT"
    assert strategy._get_final_short_exit_purpose() == "SHORT_TP_EXIT"

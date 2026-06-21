import pytest

from fixed_cycle_hedge_bot.base import StrategyContext
from fixed_cycle_hedge_bot.fixed_cycle_strategy import (
    FixedCycleHedgeStrategy,
    FixedCycleHedgeConfig,
    ShortFixedCycleHedgeStrategy,
)
from fixed_cycle_hedge_bot.models import HedgeSnapshot, RuntimeState
from fixed_cycle_hedge_bot.cycle_sequence import STEP_WAITING_FOR_PAIR_FIRST_LEG


class DummyAudit:
    def log_event(self, *_args, **_kwargs):
        pass


class DummyOrderManager:
    def __init__(self, orders):
        self._orders = orders

    def fetch_open_orders(self, *, symbol, category):
        return list(self._orders)


class DummyOrder(dict):
    pass


def make_context(symbol, category, orders):
    return StrategyContext(
        audit=DummyAudit(),
        runtime_name="fixed_cycle",
        symbol=symbol,
        category=category,
        min_order_value=1.0,
        order_manager=DummyOrderManager(orders),
    )


def make_snapshot():
    return HedgeSnapshot(
        symbol="TEST",
        current_price=0.0,
        long_qty=0.0,
        short_qty=0.0,
        long_avg=0.0,
        short_avg=0.0,
        active_orders=(),
    )


def cycle_order(order_link_id, purpose):
    base = f"{order_link_id}-1"
    return DummyOrder(
        orderLinkId=base,
        clientOrderId=base,
        purpose=purpose,
    )


def test_long_structure_guard():
    strategy = FixedCycleHedgeStrategy(FixedCycleHedgeConfig(symbol="BTCUSDT"))
    runtime_state = RuntimeState()
    state = runtime_state.strategy_state
    state["active_cycle_index"] = 1
    state["cycle_step"] = STEP_WAITING_FOR_PAIR_FIRST_LEG
    state["current_effective_cycle"] = 1
    state["next_required_purpose"] = "CYCLE_1_LONG_ADD"

    orders = [
        cycle_order("fixed_cycle-CYCLE_1_LONG_ADD", "CYCLE_1_LONG_ADD"),
        cycle_order("fixed_cycle-LONG_TP_EXIT", "LONG_TP_EXIT"),
        cycle_order("fixed_cycle-SHORT_SL_EXIT", "SHORT_SL_EXIT"),
    ]

    context = make_context("BTCUSDT", "linear", orders)
    structure_ok, _ = strategy._verify_structure_intact_before_deactivation_recovery(
        snapshot=make_snapshot(),
        runtime_state=runtime_state,
        context=context,
        purpose="LONG_TP_EXIT",
        order_link_id="test",
        exchange_order_id="exchange",
        reason="DEACTIVATED",
    )
    assert structure_ok is True


def test_short_structure_guard():
    strategy = ShortFixedCycleHedgeStrategy(FixedCycleHedgeConfig(symbol="BTCUSDT"))
    runtime_state = RuntimeState()
    state = runtime_state.strategy_state
    state["active_cycle_index"] = 1
    state["cycle_step"] = STEP_WAITING_FOR_PAIR_FIRST_LEG
    state["current_effective_cycle"] = 1
    state["next_required_purpose"] = "CYCLE_1_SHORT_REDUCE"

    orders = [
        cycle_order("fixed_cycle-CYCLE_1_SHORT_REDUCE", "CYCLE_1_SHORT_REDUCE"),
        cycle_order("fixed_cycle-LONG_TP_EXIT", "LONG_TP_EXIT"),
        cycle_order("fixed_cycle-SHORT_SL_EXIT", "SHORT_SL_EXIT"),
    ]

    context = make_context("BTCUSDT", "linear", orders)
    structure_ok, _ = strategy._verify_structure_intact_before_deactivation_recovery(
        snapshot=make_snapshot(),
        runtime_state=runtime_state,
        context=context,
        purpose="SHORT_SL_EXIT",
        order_link_id="short",
        exchange_order_id="exchange",
        reason="DEACTIVATED",
    )
    assert structure_ok is True


def test_unknown_cycle_prefers_false():
    strategy = FixedCycleHedgeStrategy(FixedCycleHedgeConfig(symbol="BTCUSDT"))
    runtime_state = RuntimeState()
    state = runtime_state.strategy_state
    state["active_cycle_index"] = 0
    state["current_effective_cycle"] = 0
    state["cycle_step"] = None
    state.pop("next_required_purpose", None)

    orders = [
        cycle_order("fixed_cycle-CYCLE_2_LONG_REDUCE", "CYCLE_2_LONG_REDUCE"),
        cycle_order("fixed_cycle-LONG_TP_EXIT", "LONG_TP_EXIT"),
        cycle_order("fixed_cycle-SHORT_SL_EXIT", "SHORT_SL_EXIT"),
    ]

    context = make_context("BTCUSDT", "linear", orders)
    structure_ok, _ = strategy._verify_structure_intact_before_deactivation_recovery(
        snapshot=make_snapshot(),
        runtime_state=runtime_state,
        context=context,
        purpose="LONG_TP_EXIT",
        order_link_id="unknown",
        exchange_order_id="unknown",
        reason="DEACTIVATED",
    )
    assert structure_ok is False

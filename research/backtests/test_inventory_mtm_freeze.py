"""Fast unit tests for the backtest-only inventory_mtm<-1 freeze policy audit.

Pure math (``inventory_mtm_freeze.py``) is tested directly; the shim
(``inventory_mtm_freeze_shim.py``) is exercised against a lightweight fake
simulator so these tests stay well under 30s with no real candle data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from fixed_cycle_hedge_bot.fixed_cycle_strategy import TpProjection
from fixed_cycle_hedge_bot.models import StrategyIntent

from research.backtests.inventory_mtm_freeze import (
    FreezeRuntimeState,
    InventoryMtmFreezeConfig,
    apply_exit_freeze_long,
    apply_exit_freeze_short,
    classify_trigger_case,
    inventory_mtm_usdt,
    is_injusdt_trade8_undercoverage,
    is_new_cycle_open_purpose,
    parse_cycle_number,
    would_increase_abs_net_exposure,
)
from research.backtests.inventory_mtm_freeze_shim import install_inventory_mtm_freeze


# ---------------------------------------------------------------------------
# Pure math: inventory_mtm_usdt
# ---------------------------------------------------------------------------


def test_inventory_mtm_usdt_formula() -> None:
    mtm = inventory_mtm_usdt(
        realized=-2.0,
        long_qty=10.0,
        long_avg=100.0,
        short_qty=5.0,
        short_avg=95.0,
        mark=90.0,
    )
    # -2 + 10*(90-100) + 5*(95-90) = -2 - 100 + 25 = -77
    assert mtm == pytest.approx(-77.0)


def test_inventory_mtm_usdt_flat_book_equals_realized() -> None:
    mtm = inventory_mtm_usdt(realized=3.5, long_qty=0.0, long_avg=0.0, short_qty=0.0, short_avg=0.0, mark=123.0)
    assert mtm == pytest.approx(3.5)


# ---------------------------------------------------------------------------
# Pure math: would_increase_abs_net_exposure
# ---------------------------------------------------------------------------


def test_would_increase_abs_net_exposure_long_add_grows_net() -> None:
    assert would_increase_abs_net_exposure(
        long_qty=10.0, short_qty=5.0, side="long", qty=3.0, reduce_only=False
    ) is True


def test_would_increase_abs_net_exposure_short_reduce_shrinks_net() -> None:
    # side=short, not reduce_only: adds to short, net = long-short shrinks.
    assert would_increase_abs_net_exposure(
        long_qty=10.0, short_qty=5.0, side="short", qty=3.0, reduce_only=False
    ) is False


def test_would_increase_abs_net_exposure_long_reduce_only_never_grows_when_net_positive() -> None:
    assert would_increase_abs_net_exposure(
        long_qty=10.0, short_qty=5.0, side="long", qty=4.0, reduce_only=True
    ) is False


def test_would_increase_abs_net_exposure_short_reduce_only_can_grow_net() -> None:
    # side=short, reduce_only: shrinks short, net = long-(short-qty) grows.
    assert would_increase_abs_net_exposure(
        long_qty=10.0, short_qty=5.0, side="short", qty=3.0, reduce_only=True
    ) is True


# ---------------------------------------------------------------------------
# Pure math: exit freeze clamps
# ---------------------------------------------------------------------------


def test_apply_exit_freeze_long_clamps_increase() -> None:
    assert apply_exit_freeze_long(raw_exit=110.0, latched_ceiling=100.0) == pytest.approx(100.0)


def test_apply_exit_freeze_long_allows_lower_exit() -> None:
    assert apply_exit_freeze_long(raw_exit=90.0, latched_ceiling=100.0) == pytest.approx(90.0)


def test_apply_exit_freeze_long_passthrough_when_not_latched() -> None:
    assert apply_exit_freeze_long(raw_exit=110.0, latched_ceiling=None) == pytest.approx(110.0)


def test_apply_exit_freeze_short_mirrors_long() -> None:
    assert apply_exit_freeze_short(raw_exit=90.0, latched_floor=100.0) == pytest.approx(100.0)
    assert apply_exit_freeze_short(raw_exit=110.0, latched_floor=100.0) == pytest.approx(110.0)
    assert apply_exit_freeze_short(raw_exit=90.0, latched_floor=None) == pytest.approx(90.0)


# ---------------------------------------------------------------------------
# Pure helpers: purpose parsing / classification / undercoverage marker
# ---------------------------------------------------------------------------


def test_parse_cycle_number() -> None:
    assert parse_cycle_number("CYCLE_3_LONG_ADD") == 3
    assert parse_cycle_number("CYCLE_12_SHORT_REDUCE") == 12
    assert parse_cycle_number("LONG_TP_EXIT") is None


def test_is_new_cycle_open_purpose_long_primary() -> None:
    assert is_new_cycle_open_purpose("CYCLE_2_LONG_ADD", primary_side="long") is True
    assert is_new_cycle_open_purpose("CYCLE_2_SHORT_REDUCE", primary_side="long") is False
    assert is_new_cycle_open_purpose("REFILL_LONG", primary_side="long") is False


def test_is_new_cycle_open_purpose_short_primary() -> None:
    assert is_new_cycle_open_purpose("CYCLE_2_SHORT_ADD", primary_side="short") is True
    assert is_new_cycle_open_purpose("CYCLE_2_LONG_REDUCE", primary_side="short") is False


@pytest.mark.parametrize(
    "baseline_is_blocker,trigger_fired,expected",
    [
        (True, True, "TP"),
        (False, True, "FP"),
        (True, False, "FN"),
        (False, False, "TN"),
    ],
)
def test_classify_trigger_case(baseline_is_blocker: bool, trigger_fired: bool, expected: str) -> None:
    assert classify_trigger_case(baseline_is_blocker=baseline_is_blocker, trigger_fired=trigger_fired) == expected


def test_injusdt_trade8_undercoverage_marker() -> None:
    assert is_injusdt_trade8_undercoverage(coin="INJUSDT", trade_number=8) is True
    assert is_injusdt_trade8_undercoverage(coin="injusdt", trade_number=8) is True
    assert is_injusdt_trade8_undercoverage(coin="INJUSDT", trade_number=7) is False
    assert is_injusdt_trade8_undercoverage(coin="APTUSDT", trade_number=8) is False


# ---------------------------------------------------------------------------
# Shim integration harness: a minimal fake simulator (no real candle data).
# ---------------------------------------------------------------------------


@dataclass
class _FakeBook:
    long_qty: float = 0.0
    short_qty: float = 0.0
    long_avg: float = 0.0
    short_avg: float = 0.0
    submitted_intents: list[StrategyIntent] = field(default_factory=list)

    def submit_intent(self, intent: StrategyIntent, *, replace: bool = True):
        self.submitted_intents.append(intent)
        order = SimpleNamespace(
            order_id=f"fake-order-{len(self.submitted_intents)}",
            side=intent.side,
            qty=intent.qty,
            reduce_only=intent.reduce_only,
            purpose=intent.purpose,
            price=None,
            trigger_price=None,
            trigger_direction=None,
            order_type=intent.order_type,
            status="NEW",
            metadata={},
            filled_qty=0.0,
            remaining_qty=intent.qty,
            created_index=0,
        )
        return order, []

    def apply_fill(self, *, order_id: str, fill_price: float, qty: float | None = None):
        raise AssertionError("_FakeBook.apply_fill should not be reached in these unit tests")


class _FakeCandle:
    def __init__(self, close: float) -> None:
        self.close = close
        self.timestamp = None


class _FakeRuntimeState:
    def __init__(self) -> None:
        self.strategy_state: dict[str, Any] = {}
        self.last_snapshot: Any = None


class _FakeStrategy:
    LONG_TP_EXIT_PURPOSE = "LONG_TP_EXIT"
    SHORT_SL_EXIT_PURPOSE = "SHORT_SL_EXIT"

    def __init__(self, *, primary_side: str = "long") -> None:
        self._primary_side = primary_side
        self.tp_projection: TpProjection | None = None
        self.tp_projection_calls = 0

    def _get_primary_position_side(self) -> str:
        return self._primary_side

    def _calculate_tp_projection(
        self,
        break_even_price: float,
        snapshot: Any = None,
        runtime_state: Any = None,
    ) -> TpProjection:
        self.tp_projection_calls += 1
        assert self.tp_projection is not None
        return self.tp_projection


class _FakeFill:
    def __init__(self, *, purpose: str, closed_pnl: float = 0.0, exec_price: float = 0.0) -> None:
        self.purpose = purpose
        self.exec_price = exec_price
        self.metadata = {"closed_pnl": closed_pnl}


class _FakeProcessCandleResult:
    def __init__(self, *, candle: _FakeCandle, candle_fills: list[_FakeFill] | None = None) -> None:
        self.candle = candle
        self.candle_fills = list(candle_fills or [])


class _FakeSim:
    def __init__(self, *, primary_side: str = "long") -> None:
        self.book = _FakeBook()
        self.strategy = _FakeStrategy(primary_side=primary_side)
        self.runtime_state = _FakeRuntimeState()
        self.intent_filter = None
        self.candle_index = 0
        self.candle = _FakeCandle(100.0)
        self._queued_entry_fills: list[_FakeFill] = []
        self._queued_candle_fills: list[_FakeFill] = []
        self.refresh_calls = 0

    def run_entry_smoke(self):
        return SimpleNamespace(entry_fills=list(self._queued_entry_fills))

    def process_candle(self, candle: _FakeCandle, **kwargs: Any) -> _FakeProcessCandleResult:
        self.candle = candle
        result = _FakeProcessCandleResult(candle=candle, candle_fills=list(self._queued_candle_fills))
        self._queued_candle_fills = []
        return result

    def _refresh_snapshot_from_book(self, *, source: str) -> None:
        self.refresh_calls += 1


def _fake_tp_projection(tp_price: float) -> TpProjection:
    return TpProjection(
        tp_price=tp_price,
        target_delta_usdt=0.0,
        expected_total_net_after_exit=0.0,
        target_total_profit_usdt=0.0,
        required_profit_to_cover_loss=0.0,
        min_profit_target_usdt=0.0,
        min_required_total_usdt=0.0,
        components=None,  # not read by the freeze shim
        fee_rate=0.00055,
        entry_fee_usdt=0.0,
        close_fee_usdt=0.0,
        pending_cycle_loss_usdt=0.0,
        realized_cycle_net=0.0,
    )


def _fire_trigger(
    sim: _FakeSim,
    *,
    candle_index: int,
    mark: float,
    long_qty: float,
    short_qty: float,
    long_avg: float = 100.0,
    short_avg: float = 100.0,
) -> None:
    """Drive book state to the given mark/qty and process one candle.

    Defaults (``long_avg``/``short_avg`` = 100.0) make ``mark`` directly
    control the sign/magnitude of the resulting inventory MTM for the caller.
    """
    sim.book.long_qty = long_qty
    sim.book.long_avg = long_avg
    sim.book.short_qty = short_qty
    sim.book.short_avg = short_avg
    sim.candle_index = candle_index
    sim.process_candle(_FakeCandle(mark))


# ---------------------------------------------------------------------------
# A0 / None: strict no-op
# ---------------------------------------------------------------------------


def test_a0_variant_is_noop_and_omits_wraps() -> None:
    sim = _FakeSim()
    original_process_candle = sim.process_candle
    original_run_entry_smoke = sim.run_entry_smoke

    install_inventory_mtm_freeze(sim, InventoryMtmFreezeConfig(variant="A0"))

    assert sim.process_candle == original_process_candle
    assert sim.run_entry_smoke == original_run_entry_smoke
    assert sim.intent_filter is None
    assert sim.strategy._backtest_inventory_mtm_freeze_variant == "A0"
    assert sim.strategy._backtest_inventory_mtm_trigger_event is None
    assert sim.strategy._backtest_inventory_mtm_policy_actions == []
    assert sim.strategy._backtest_inventory_mtm_freeze_state is None


def test_none_config_is_also_noop() -> None:
    sim = _FakeSim()
    original_process_candle = sim.process_candle

    install_inventory_mtm_freeze(sim, None)

    assert sim.process_candle == original_process_candle
    assert sim.strategy._backtest_inventory_mtm_freeze_variant == "A0"


# ---------------------------------------------------------------------------
# Trigger detection (A1, arbitrary non-A0 variant exercises the same trigger path)
# ---------------------------------------------------------------------------


def test_trigger_fires_on_first_causal_candle_below_threshold() -> None:
    sim = _FakeSim()
    install_inventory_mtm_freeze(sim, InventoryMtmFreezeConfig(variant="A1"))

    # Candle 1: mtm = 0 + 10*(99-100) + 0 = -10 < -1 -> fires immediately.
    _fire_trigger(sim, candle_index=1, mark=99.0, long_qty=10.0, short_qty=0.0)

    event = sim.strategy._backtest_inventory_mtm_trigger_event
    assert event is not None
    assert event["trigger_candle"] == 1
    assert event["trigger_mtm"] == pytest.approx(-10.0)
    assert len(sim.strategy._backtest_inventory_mtm_trigger_events) == 1


def test_no_trigger_when_mtm_above_threshold() -> None:
    sim = _FakeSim()
    install_inventory_mtm_freeze(sim, InventoryMtmFreezeConfig(variant="A1"))

    # mtm = 10*(100.05-100) = 0.5 >= -1 -> no trigger.
    _fire_trigger(sim, candle_index=1, mark=100.05, long_qty=10.0, short_qty=0.0)

    assert sim.strategy._backtest_inventory_mtm_trigger_event is None


def test_no_trigger_after_max_trigger_candle() -> None:
    sim = _FakeSim()
    install_inventory_mtm_freeze(
        sim, InventoryMtmFreezeConfig(variant="A1", max_trigger_candle=500)
    )

    # candle_index=501 is outside the eligible trigger window even though mtm<-1.
    _fire_trigger(sim, candle_index=501, mark=50.0, long_qty=10.0, short_qty=0.0)

    assert sim.strategy._backtest_inventory_mtm_trigger_event is None


def test_trigger_latches_once_further_declines_ignored() -> None:
    sim = _FakeSim()
    install_inventory_mtm_freeze(sim, InventoryMtmFreezeConfig(variant="A1"))

    _fire_trigger(sim, candle_index=1, mark=99.0, long_qty=10.0, short_qty=0.0)
    first_event = sim.strategy._backtest_inventory_mtm_trigger_event
    assert first_event["trigger_candle"] == 1

    # Further decline on a later candle must not overwrite the latched trigger.
    sim.candle_index = 2
    sim.process_candle(_FakeCandle(50.0))

    assert sim.strategy._backtest_inventory_mtm_trigger_event is first_event
    assert len(sim.strategy._backtest_inventory_mtm_trigger_events) == 1


def test_realized_pnl_accumulates_from_entry_and_candle_fills() -> None:
    sim = _FakeSim()
    sim._queued_entry_fills = [_FakeFill(purpose="INITIAL_LONG_ENTRY", closed_pnl=0.0)]
    install_inventory_mtm_freeze(sim, InventoryMtmFreezeConfig(variant="A1"))
    sim.run_entry_smoke()

    sim.book.long_qty = 10.0
    sim.book.long_avg = 100.0
    sim.book.short_qty = 0.0
    sim._queued_candle_fills = [_FakeFill(purpose="CYCLE_1_SHORT_REDUCE", closed_pnl=-3.0)]
    sim.candle_index = 1
    sim.process_candle(_FakeCandle(100.0))

    state: FreezeRuntimeState = sim.strategy._backtest_inventory_mtm_freeze_state
    assert state.realized_pnl == pytest.approx(-3.0)


# ---------------------------------------------------------------------------
# A1: freeze new cycles
# ---------------------------------------------------------------------------


def test_a1_blocks_new_cycle_only_after_trigger() -> None:
    sim = _FakeSim()
    install_inventory_mtm_freeze(sim, InventoryMtmFreezeConfig(variant="A1"))

    intent = StrategyIntent(side="long", qty=1.0, purpose="CYCLE_3_LONG_ADD")
    assert sim.intent_filter(intent) is True  # not triggered yet -> allowed

    _fire_trigger(sim, candle_index=1, mark=50.0, long_qty=10.0, short_qty=0.0)

    assert sim.intent_filter(intent) is False  # triggered -> new cycle open blocked
    # Non-cycle-open purposes remain unaffected by A1.
    other_intent = StrategyIntent(side="short", qty=1.0, purpose="CYCLE_3_SHORT_REDUCE")
    assert sim.intent_filter(other_intent) is True

    state: FreezeRuntimeState = sim.strategy._backtest_inventory_mtm_freeze_state
    blocked_actions = [a for a in state.policy_actions if a["action"] == "block_new_cycle"]
    assert len(blocked_actions) == 1
    assert blocked_actions[0]["purpose"] == "CYCLE_3_LONG_ADD"


# ---------------------------------------------------------------------------
# A2: freeze exposure growth
# ---------------------------------------------------------------------------


def test_a2_allows_reduce_blocks_growth() -> None:
    sim = _FakeSim()
    install_inventory_mtm_freeze(sim, InventoryMtmFreezeConfig(variant="A2"))
    _fire_trigger(sim, candle_index=1, mark=50.0, long_qty=10.0, short_qty=5.0)

    grow_intent = StrategyIntent(side="long", qty=3.0, purpose="CYCLE_3_LONG_ADD", reduce_only=False)
    assert sim.intent_filter(grow_intent) is False

    shrink_intent = StrategyIntent(side="short", qty=3.0, purpose="CYCLE_3_SHORT_REDUCE", reduce_only=False)
    assert sim.intent_filter(shrink_intent) is True

    state: FreezeRuntimeState = sim.strategy._backtest_inventory_mtm_freeze_state
    blocked = [a for a in state.policy_actions if a["action"] == "block_exposure_growth"]
    assert len(blocked) == 1


# ---------------------------------------------------------------------------
# A3: freeze exit increases (long + short mirror)
# ---------------------------------------------------------------------------


def test_a3_allows_lower_or_equal_exit_blocks_raise_above_latch() -> None:
    sim = _FakeSim()
    sim.runtime_state.strategy_state["latest_tp_price"] = 100.0
    install_inventory_mtm_freeze(sim, InventoryMtmFreezeConfig(variant="A3"))

    _fire_trigger(sim, candle_index=1, mark=50.0, long_qty=10.0, short_qty=0.0)
    state: FreezeRuntimeState = sim.strategy._backtest_inventory_mtm_freeze_state
    assert state.latched_exit_ceiling == pytest.approx(100.0)

    sim.strategy.tp_projection = _fake_tp_projection(110.0)
    projection = sim.strategy._calculate_tp_projection(1.0)
    assert projection.tp_price == pytest.approx(100.0)
    assert state.exit_increases_after_trigger >= 1

    sim.strategy.tp_projection = _fake_tp_projection(90.0)
    projection = sim.strategy._calculate_tp_projection(1.0)
    assert projection.tp_price == pytest.approx(90.0)


def test_a3_short_primary_mirror_uses_floor() -> None:
    sim = _FakeSim(primary_side="short")
    install_inventory_mtm_freeze(sim, InventoryMtmFreezeConfig(variant="A3"))

    # short-primary loss requires mark to rise above short_avg (default 100.0).
    _fire_trigger(sim, candle_index=1, mark=150.0, long_qty=0.0, short_qty=10.0)

    sim.strategy.tp_projection = _fake_tp_projection(80.0)
    first = sim.strategy._calculate_tp_projection(1.0)
    # No active exit resolvable for the fake strategy/snapshot -> lazily latched.
    assert first.tp_price == pytest.approx(80.0)

    state: FreezeRuntimeState = sim.strategy._backtest_inventory_mtm_freeze_state
    assert state.latched_exit_floor == pytest.approx(80.0)

    sim.strategy.tp_projection = _fake_tp_projection(70.0)  # would lower below floor -> blocked
    second = sim.strategy._calculate_tp_projection(1.0)
    assert second.tp_price == pytest.approx(80.0)

    sim.strategy.tp_projection = _fake_tp_projection(90.0)  # improves upward -> allowed
    third = sim.strategy._calculate_tp_projection(1.0)
    assert third.tp_price == pytest.approx(90.0)


# ---------------------------------------------------------------------------
# A4: combined A1 + A2 + A3
# ---------------------------------------------------------------------------


def test_a4_combines_all_three_freezes() -> None:
    sim = _FakeSim()
    sim.runtime_state.strategy_state["latest_tp_price"] = 100.0
    install_inventory_mtm_freeze(sim, InventoryMtmFreezeConfig(variant="A4"))

    _fire_trigger(sim, candle_index=1, mark=50.0, long_qty=10.0, short_qty=2.0)

    cycle_intent = StrategyIntent(side="long", qty=1.0, purpose="CYCLE_5_LONG_ADD")
    assert sim.intent_filter(cycle_intent) is False

    grow_intent = StrategyIntent(side="long", qty=1.0, purpose="REFILL_LONG")
    assert sim.intent_filter(grow_intent) is False

    shrink_intent = StrategyIntent(side="short", qty=1.0, purpose="CYCLE_5_SHORT_REDUCE")
    assert sim.intent_filter(shrink_intent) is True

    sim.strategy.tp_projection = _fake_tp_projection(150.0)
    projection = sim.strategy._calculate_tp_projection(1.0)
    assert projection.tp_price == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Filter side effects: never touches order/eligibility state.
# ---------------------------------------------------------------------------


def test_intent_filter_does_not_mutate_intent_or_touch_eligibility() -> None:
    sim = _FakeSim()
    install_inventory_mtm_freeze(sim, InventoryMtmFreezeConfig(variant="A1"))
    _fire_trigger(sim, candle_index=1, mark=50.0, long_qty=10.0, short_qty=0.0)

    intent = StrategyIntent(side="long", qty=2.0, purpose="CYCLE_9_LONG_ADD", price=None, trigger_price=None)
    before = (intent.side, intent.qty, intent.purpose, intent.price, intent.trigger_price)
    result_first = sim.intent_filter(intent)
    result_second = sim.intent_filter(intent)
    after = (intent.side, intent.qty, intent.purpose, intent.price, intent.trigger_price)

    assert before == after
    assert result_first == result_second is False
    assert not hasattr(intent, "eligible_from_candle_index")

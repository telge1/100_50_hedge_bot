import logging
import threading

from emergency_100.final_hedge_strategy import PSRHStrategy
from strategy.config import StrategyConfig
from strategy.position_manager import PositionManager
from strategy.risk_manager import RiskManager
from strategy.state_machine import StateMachine, StrategyState


def build_strategy_for_update_state(
    *,
    long_size: float,
    long_avg: float,
    short_size: float,
    short_avg: float,
    realized_long: float = 0.0,
    realized_short: float = 0.0,
) -> PSRHStrategy:
    strategy = PSRHStrategy.__new__(PSRHStrategy)
    strategy.config = StrategyConfig()
    strategy.config.min_order_value = 1.0
    strategy.config.default_symbol = "BTCUSDT"
    strategy.config.category = "linear"
    strategy.position_manager = PositionManager()
    strategy.position_manager.sync_positions(long_size, long_avg, short_size, short_avg)
    strategy.state_machine = StateMachine()
    strategy.state_machine.transition(StrategyState.NORMAL_FLOW)
    strategy.risk_manager = RiskManager(strategy.config)
    strategy.logger = logging.getLogger("test.final_hedge_strategy.flow_guards")
    strategy.order_manager = None
    strategy._position_sync_lock = threading.Lock()
    strategy._init_lock = threading.Lock()
    strategy._order_lock = threading.Lock()
    strategy.initialized = True
    strategy._long_heal_adds = 0
    strategy._short_heal_adds = 0
    strategy._spread_healing_active = False
    strategy._realized_long_pnl_total = realized_long
    strategy._realized_short_pnl_total = realized_short
    strategy._long_adds_in_cycle = 0
    strategy._short_adds_in_cycle = 0
    strategy._last_relevant_high = None
    strategy._last_relevant_low = None
    strategy._pending_rebuild_side = None
    strategy._pending_failover_side = None
    strategy._wait_reference_price = None
    strategy._last_structure_event = None
    strategy._aggressive_down_heal_initial_short_size = None
    strategy._aggressive_down_heal_reference_price = None
    strategy._aggressive_down_heal_phase_completed = False
    strategy._phase2_short_profit_budget_reserved = 0.0
    strategy._phase3_long_target_reference_size = None
    strategy._phase4_short_target_reference_size = None
    strategy._preplaced_heal_orders_armed = False
    strategy._preplaced_heal_generation = 0
    strategy._active_preplaced_heal_long_client_id = None
    strategy._active_preplaced_heal_short_client_id = None
    strategy._preplaced_heal_rearm_in_progress = False
    strategy.active_orders = {}
    return strategy


def test_confirmed_up_move_returns_single_normal_reduce_long_and_wait_pullback() -> None:
    strategy = build_strategy_for_update_state(
        long_size=10.0,
        long_avg=100.0,
        short_size=10.0,
        short_avg=99.0,
    )
    strategy._last_relevant_high = 100.0
    strategy._last_relevant_low = 100.0

    intents = strategy.update_state(price=101.0, spread=abs(strategy.calculate_hedge_spread()))

    assert len(intents) == 1
    assert intents[0].purpose == "normal_reduce_long"
    assert strategy.state_machine.state == StrategyState.WAIT_PULLBACK
    assert strategy._pending_rebuild_side == "long"
    assert strategy._wait_reference_price == 101.0


def test_confirmed_down_move_returns_single_normal_reduce_short_and_wait_pullback() -> None:
    strategy = build_strategy_for_update_state(
        long_size=10.0,
        long_avg=100.0,
        short_size=10.0,
        short_avg=99.0,
    )
    strategy._last_relevant_high = 100.0
    strategy._last_relevant_low = 99.0

    intents = strategy.update_state(price=98.0, spread=abs(strategy.calculate_hedge_spread()))

    assert len(intents) == 1
    assert intents[0].purpose == "normal_reduce_short"
    assert strategy.state_machine.state == StrategyState.WAIT_PULLBACK
    assert strategy._pending_rebuild_side == "short"
    assert strategy._wait_reference_price == 98.0


def test_healing_flow_still_returns_single_intent() -> None:
    strategy = build_strategy_for_update_state(
        long_size=10.0,
        long_avg=100.0,
        short_size=10.0,
        short_avg=96.0,
    )
    strategy._last_relevant_high = 100.0
    strategy._last_relevant_low = 100.0

    intents = strategy.update_state(price=95.0, spread=abs(strategy.calculate_hedge_spread()))

    assert len(intents) == 1
    assert intents[0].purpose == "spread_heal_long"


def test_spread_heal_short_uses_configured_fine_heal_size_pct() -> None:
    strategy = build_strategy_for_update_state(
        long_size=10.0,
        long_avg=100.0,
        short_size=10.0,
        short_avg=96.0,
    )
    strategy.config.enable_fine_heal_phase = True
    strategy.config.fine_heal_size_pct = 0.15

    intents = strategy.update_state(price=97.5, spread=abs(strategy.calculate_hedge_spread()))

    assert len(intents) == 1
    assert intents[0].purpose == "spread_heal_short"
    assert intents[0].qty == 1.5
    assert intents[0].reduce_only is True


def test_spread_heal_short_uses_short_size_as_qty_basis() -> None:
    strategy = build_strategy_for_update_state(
        long_size=12.0,
        long_avg=100.0,
        short_size=8.0,
        short_avg=96.0,
    )
    strategy.config.enable_fine_heal_phase = True
    strategy.config.fine_heal_size_pct = 0.15

    intents = strategy.update_state(price=97.5, spread=abs(strategy.calculate_hedge_spread()))

    assert len(intents) == 1
    assert intents[0].purpose == "spread_heal_short"
    assert abs(intents[0].qty - 1.2) < 1e-9
    assert intents[0].reduce_only is True


def test_spread_heal_short_uses_current_short_size_when_long_is_larger() -> None:
    strategy = build_strategy_for_update_state(
        long_size=20.0,
        long_avg=100.0,
        short_size=1.0,
        short_avg=96.0,
    )
    strategy.config.enable_fine_heal_phase = True
    strategy.config.fine_heal_size_pct = 0.15

    intents = strategy.update_state(price=97.5, spread=abs(strategy.calculate_hedge_spread()))

    assert len(intents) == 1
    assert intents[0].purpose == "spread_heal_short"
    assert abs(intents[0].qty - 0.15) < 1e-9
    assert intents[0].reduce_only is True


def test_basket_exit_allows_multi_intent_only_for_explicit_exit_flow() -> None:
    strategy = build_strategy_for_update_state(
        long_size=10.0,
        long_avg=100.0,
        short_size=10.0,
        short_avg=99.0,
        realized_long=20.0,
        realized_short=0.0,
    )
    strategy._last_relevant_high = 100.0
    strategy._last_relevant_low = 100.0

    intents = strategy.update_state(price=100.0, spread=abs(strategy.calculate_hedge_spread()))

    assert len(intents) == 2
    assert {intent.purpose for intent in intents} == {"basket_exit"}
    assert {intent.side for intent in intents} == {"long", "short"}


def test_aggressive_phase_disabled_keeps_existing_heal_behavior() -> None:
    strategy = build_strategy_for_update_state(
        long_size=10.0,
        long_avg=100.0,
        short_size=10.0,
        short_avg=96.0,
    )
    strategy._last_relevant_high = 100.0
    strategy._last_relevant_low = 100.0
    strategy.config.enable_aggressive_heal_phase = False

    intents = strategy.update_state(price=97.0, spread=abs(strategy.calculate_hedge_spread()))

    assert len(intents) == 1
    assert intents[0].purpose == "spread_heal_short"
    assert intents[0].reduce_only is True


def test_aggressive_down_heal_triggers_exactly_one_short_close_with_short_based_qty() -> None:
    strategy = build_strategy_for_update_state(
        long_size=10.0,
        long_avg=100.0,
        short_size=10.0,
        short_avg=98.0,
    )
    strategy.config.enable_aggressive_heal_phase = True
    strategy.config.aggressive_down_heal_step_pct = 0.01
    strategy.config.aggressive_down_heal_size_pct = 0.20

    first_pass = strategy.update_state(price=99.0, spread=abs(strategy.calculate_hedge_spread()))
    second_pass = strategy.update_state(price=98.0, spread=abs(strategy.calculate_hedge_spread()))

    assert first_pass == []
    assert len(second_pass) == 1
    assert second_pass[0].purpose == "aggressive_down_heal_short"
    assert second_pass[0].qty == 2.0
    assert strategy._aggressive_down_heal_reference_price == 98.0


def test_aggressive_down_heal_handles_multiple_confirmed_steps() -> None:
    strategy = build_strategy_for_update_state(
        long_size=10.0,
        long_avg=100.0,
        short_size=10.0,
        short_avg=98.0,
    )
    strategy.config.enable_aggressive_heal_phase = True

    assert strategy.update_state(price=99.0, spread=abs(strategy.calculate_hedge_spread())) == []
    first_step = strategy.update_state(price=98.0, spread=abs(strategy.calculate_hedge_spread()))
    strategy.position_manager.sync_positions(10.0, 100.0, 8.0, 98.0)
    second_step = strategy.update_state(price=97.0, spread=abs(strategy.calculate_hedge_spread()))

    assert len(first_step) == 1
    assert len(second_step) == 1
    assert first_step[0].purpose == "aggressive_down_heal_short"
    assert second_step[0].purpose == "aggressive_down_heal_short"
    assert second_step[0].qty == 1.6


def test_aggressive_down_heal_stops_after_original_short_is_gone() -> None:
    strategy = build_strategy_for_update_state(
        long_size=10.0,
        long_avg=100.0,
        short_size=10.0,
        short_avg=98.0,
    )
    strategy.config.enable_aggressive_heal_phase = True

    assert strategy.update_state(price=99.0, spread=abs(strategy.calculate_hedge_spread())) == []
    assert strategy.update_state(price=98.0, spread=abs(strategy.calculate_hedge_spread()))
    strategy.position_manager.sync_positions(10.0, 100.0, 0.0, 98.0)

    intents = strategy.update_state(price=97.0, spread=abs(strategy.calculate_hedge_spread()))

    assert all(intent.purpose != "aggressive_down_heal_short" for intent in intents)
    assert strategy._aggressive_down_heal_initial_short_size is None
    assert strategy._aggressive_down_heal_reference_price is None


def test_aggressive_down_heal_does_not_collide_with_preplaced_mode() -> None:
    strategy = build_strategy_for_update_state(
        long_size=10.0,
        long_avg=100.0,
        short_size=10.0,
        short_avg=98.0,
    )
    strategy.config.enable_aggressive_heal_phase = True
    strategy.config.preplaced_heal_enabled = True
    armed = {"called": False}
    strategy._arm_preplaced_heal_orders = lambda: armed.__setitem__("called", True)

    intents = strategy.update_state(price=98.0, spread=abs(strategy.calculate_hedge_spread()))

    assert intents == []
    assert armed["called"] is False
    assert strategy._aggressive_down_heal_initial_short_size == 10.0


def test_phase2_disabled_keeps_existing_behavior() -> None:
    strategy = build_strategy_for_update_state(
        long_size=10.0,
        long_avg=100.0,
        short_size=0.0,
        short_avg=95.0,
        realized_short=10.0,
    )
    strategy.config.enable_phase2_short_profit_long_reduce = False
    strategy._aggressive_down_heal_phase_completed = True

    intents = strategy.update_state(price=90.0, spread=abs(strategy.calculate_hedge_spread()))

    assert all(intent.purpose != "phase2_long_reduce_from_short_profit" for intent in intents)


def test_phase2_does_not_trigger_before_phase1_completed() -> None:
    strategy = build_strategy_for_update_state(
        long_size=10.0,
        long_avg=100.0,
        short_size=0.0,
        short_avg=95.0,
        realized_short=10.0,
    )
    strategy.config.enable_phase2_short_profit_long_reduce = True
    strategy._aggressive_down_heal_phase_completed = False

    intents = strategy.update_state(price=90.0, spread=abs(strategy.calculate_hedge_spread()))

    assert all(intent.purpose != "phase2_long_reduce_from_short_profit" for intent in intents)


def test_phase2_does_not_trigger_without_realized_short_profit() -> None:
    strategy = build_strategy_for_update_state(
        long_size=10.0,
        long_avg=100.0,
        short_size=0.0,
        short_avg=95.0,
        realized_short=0.0,
    )
    strategy.config.enable_phase2_short_profit_long_reduce = True
    strategy._aggressive_down_heal_phase_completed = True

    intents = strategy.update_state(price=90.0, spread=abs(strategy.calculate_hedge_spread()))

    assert all(intent.purpose != "phase2_long_reduce_from_short_profit" for intent in intents)


def test_phase2_does_not_trigger_when_price_not_below_long_avg() -> None:
    strategy = build_strategy_for_update_state(
        long_size=10.0,
        long_avg=100.0,
        short_size=0.0,
        short_avg=95.0,
        realized_short=10.0,
    )
    strategy.config.enable_phase2_short_profit_long_reduce = True
    strategy._aggressive_down_heal_phase_completed = True

    intents = strategy.update_state(price=100.0, spread=abs(strategy.calculate_hedge_spread()))

    assert all(intent.purpose != "phase2_long_reduce_from_short_profit" for intent in intents)


def test_phase2_builds_budget_covered_partial_long_reduce() -> None:
    strategy = build_strategy_for_update_state(
        long_size=10.0,
        long_avg=100.0,
        short_size=0.0,
        short_avg=95.0,
        realized_short=10.0,
    )
    strategy.config.enable_phase2_short_profit_long_reduce = True
    strategy._aggressive_down_heal_phase_completed = True

    intents = strategy.update_state(price=98.0, spread=abs(strategy.calculate_hedge_spread()))

    assert len(intents) == 1
    assert intents[0].purpose == "phase2_long_reduce_from_short_profit"
    assert abs(intents[0].qty - 5.0) < 1e-9
    assert strategy._phase2_short_profit_budget_reserved == 0.0


def test_phase2_long_reduce_qty_is_capped_to_current_long_size() -> None:
    strategy = build_strategy_for_update_state(
        long_size=5.0,
        long_avg=100.0,
        short_size=0.0,
        short_avg=95.0,
        realized_short=50.0,
    )
    strategy.config.enable_phase2_short_profit_long_reduce = True
    strategy._aggressive_down_heal_phase_completed = True

    intent = strategy._build_phase2_long_reduce_from_short_profit_intent(price=99.0)

    assert intent is not None
    assert intent.purpose == "phase2_long_reduce_from_short_profit"
    assert abs(intent.qty - 5.0) < 1e-9


def test_phase2_does_not_collide_with_preplaced_mode() -> None:
    strategy = build_strategy_for_update_state(
        long_size=10.0,
        long_avg=100.0,
        short_size=0.0,
        short_avg=95.0,
        realized_short=10.0,
    )
    strategy.config.enable_phase2_short_profit_long_reduce = True
    strategy.config.preplaced_heal_enabled = True
    strategy._aggressive_down_heal_phase_completed = True

    intents = strategy.update_state(price=98.0, spread=abs(strategy.calculate_hedge_spread()))

    assert len(intents) == 1
    assert intents[0].purpose == "phase2_long_reduce_from_short_profit"
    assert strategy._phase2_long_reduce_ready() is True


def test_phase3_disabled_keeps_existing_behavior() -> None:
    strategy = build_strategy_for_update_state(
        long_size=6.0,
        long_avg=100.0,
        short_size=0.0,
        short_avg=95.0,
    )
    strategy.config.enable_phase3_long_rebuild = False
    strategy._phase3_long_target_reference_size = 10.0

    intents = strategy.update_state(price=95.0, spread=abs(strategy.calculate_hedge_spread()))

    assert all(intent.purpose != "phase3_long_rebuild" for intent in intents)


def test_phase3_does_not_trigger_when_current_long_at_or_above_target() -> None:
    strategy = build_strategy_for_update_state(
        long_size=10.0,
        long_avg=100.0,
        short_size=0.0,
        short_avg=95.0,
    )
    strategy.config.enable_phase3_long_rebuild = True
    strategy._phase3_long_target_reference_size = 10.0

    intents = strategy.update_state(price=95.0, spread=abs(strategy.calculate_hedge_spread()))

    assert all(intent.purpose != "phase3_long_rebuild" for intent in intents)


def test_phase3_triggers_when_current_long_below_target() -> None:
    strategy = build_strategy_for_update_state(
        long_size=6.0,
        long_avg=100.0,
        short_size=0.0,
        short_avg=95.0,
    )
    strategy.config.enable_phase3_long_rebuild = True
    strategy._phase3_long_target_reference_size = 10.0

    intents = strategy.update_state(price=95.0, spread=abs(strategy.calculate_hedge_spread()))

    assert len(intents) == 1
    assert intents[0].purpose == "phase3_long_rebuild"


def test_phase3_qty_is_capped_to_missing_long_amount() -> None:
    strategy = build_strategy_for_update_state(
        long_size=9.5,
        long_avg=100.0,
        short_size=0.0,
        short_avg=95.0,
    )
    strategy.config.enable_phase3_long_rebuild = True
    strategy._phase3_long_target_reference_size = 10.0

    intent = strategy._build_phase3_long_rebuild_intent(price=95.0)

    assert intent is not None
    assert intent.purpose == "phase3_long_rebuild"
    assert abs(intent.qty - 0.5) < 1e-9


def test_phase3_target_qty_uses_reference_size_and_target_pct() -> None:
    strategy = build_strategy_for_update_state(
        long_size=6.0,
        long_avg=100.0,
        short_size=0.0,
        short_avg=95.0,
    )
    strategy.config.enable_phase3_long_rebuild = True
    strategy.config.long_rebuild_target_pct = 1.2
    strategy._phase3_long_target_reference_size = 10.0

    assert abs(strategy._phase3_target_long_qty() - 12.0) < 1e-9


def test_phase3_does_not_collide_with_preplaced_mode() -> None:
    strategy = build_strategy_for_update_state(
        long_size=6.0,
        long_avg=100.0,
        short_size=0.0,
        short_avg=95.0,
    )
    strategy.config.enable_phase3_long_rebuild = True
    strategy.config.preplaced_heal_enabled = True
    strategy._phase3_long_target_reference_size = 10.0

    intents = strategy.update_state(price=95.0, spread=abs(strategy.calculate_hedge_spread()))

    assert len(intents) == 1
    assert intents[0].purpose == "phase3_long_rebuild"
    assert strategy._phase3_long_rebuild_ready() is True


def test_phase3_reference_is_not_set_after_aggressive_phase_started() -> None:
    strategy = build_strategy_for_update_state(
        long_size=6.0,
        long_avg=100.0,
        short_size=5.0,
        short_avg=95.0,
    )
    strategy._aggressive_down_heal_initial_short_size = 5.0
    strategy._phase3_long_target_reference_size = None

    strategy._ensure_phase3_long_target_reference()

    assert strategy._phase3_long_target_reference_size is None


def test_phase4_disabled_keeps_existing_behavior() -> None:
    strategy = build_strategy_for_update_state(
        long_size=10.0,
        long_avg=95.0,
        short_size=6.0,
        short_avg=100.0,
    )
    strategy.config.enable_phase4_short_rebuild = False
    strategy._phase3_long_target_reference_size = 10.0
    strategy._phase4_short_target_reference_size = 10.0

    intents = strategy.update_state(price=101.0, spread=abs(strategy.calculate_hedge_spread()))

    assert all(intent.purpose != "phase4_short_rebuild" for intent in intents)


def test_phase4_does_not_trigger_when_long_target_not_reached() -> None:
    strategy = build_strategy_for_update_state(
        long_size=8.0,
        long_avg=95.0,
        short_size=6.0,
        short_avg=100.0,
    )
    strategy.config.enable_phase4_short_rebuild = True
    strategy._phase3_long_target_reference_size = 10.0
    strategy._phase4_short_target_reference_size = 10.0

    intents = strategy.update_state(price=101.0, spread=abs(strategy.calculate_hedge_spread()))

    assert all(intent.purpose != "phase4_short_rebuild" for intent in intents)


def test_phase4_does_not_trigger_when_current_short_at_or_above_target() -> None:
    strategy = build_strategy_for_update_state(
        long_size=10.0,
        long_avg=95.0,
        short_size=10.0,
        short_avg=100.0,
    )
    strategy.config.enable_phase4_short_rebuild = True
    strategy._phase3_long_target_reference_size = 10.0
    strategy._phase4_short_target_reference_size = 10.0

    intents = strategy.update_state(price=101.0, spread=abs(strategy.calculate_hedge_spread()))

    assert all(intent.purpose != "phase4_short_rebuild" for intent in intents)


def test_phase4_triggers_when_long_target_reached_and_short_below_target() -> None:
    strategy = build_strategy_for_update_state(
        long_size=10.0,
        long_avg=108.0,
        short_size=6.0,
        short_avg=100.0,
    )
    strategy.config.enable_phase4_short_rebuild = True
    strategy._phase3_long_target_reference_size = 10.0
    strategy._phase4_short_target_reference_size = 10.0

    intents = strategy.update_state(price=101.0, spread=abs(strategy.calculate_hedge_spread()))

    assert len(intents) == 1
    assert intents[0].purpose == "phase4_short_rebuild"


def test_phase4_qty_is_capped_to_missing_short_amount() -> None:
    strategy = build_strategy_for_update_state(
        long_size=10.0,
        long_avg=95.0,
        short_size=9.5,
        short_avg=100.0,
    )
    strategy.config.enable_phase4_short_rebuild = True
    strategy._phase3_long_target_reference_size = 10.0
    strategy._phase4_short_target_reference_size = 10.0

    intent = strategy._build_phase4_short_rebuild_intent(price=101.0)

    assert intent is not None
    assert intent.purpose == "phase4_short_rebuild"
    assert abs(intent.qty - 0.5) < 1e-9


def test_phase4_target_qty_uses_reference_size_and_target_pct() -> None:
    strategy = build_strategy_for_update_state(
        long_size=10.0,
        long_avg=95.0,
        short_size=6.0,
        short_avg=100.0,
    )
    strategy.config.enable_phase4_short_rebuild = True
    strategy.config.short_rebuild_target_pct = 1.2
    strategy._phase4_short_target_reference_size = 10.0

    assert abs(strategy._phase4_target_short_qty() - 12.0) < 1e-9


def test_phase4_does_not_collide_with_preplaced_mode() -> None:
    strategy = build_strategy_for_update_state(
        long_size=10.0,
        long_avg=108.0,
        short_size=6.0,
        short_avg=100.0,
    )
    strategy.config.enable_phase4_short_rebuild = True
    strategy.config.preplaced_heal_enabled = True
    strategy._phase3_long_target_reference_size = 10.0
    strategy._phase4_short_target_reference_size = 10.0
    armed = {"called": False}
    strategy._arm_preplaced_heal_orders = lambda: armed.__setitem__("called", True)

    intents = strategy.update_state(price=101.0, spread=abs(strategy.calculate_hedge_spread()))

    assert len(intents) == 1
    assert intents[0].purpose == "phase4_short_rebuild"
    assert strategy._phase4_short_rebuild_ready() is True
    assert armed["called"] is False


def test_phase1_priority_blocks_later_phases() -> None:
    strategy = build_strategy_for_update_state(
        long_size=10.0,
        long_avg=100.0,
        short_size=10.0,
        short_avg=98.0,
        realized_short=10.0,
    )
    strategy.config.enable_aggressive_heal_phase = True
    strategy.config.enable_phase3_long_rebuild = True
    strategy.config.enable_phase4_short_rebuild = True
    strategy.config.short_rebuild_target_pct = 1.2
    strategy._aggressive_down_heal_initial_short_size = 10.0
    strategy._aggressive_down_heal_reference_price = 100.0
    strategy._phase3_long_target_reference_size = 10.0
    strategy._phase4_short_target_reference_size = 10.0

    intents = strategy.update_state(price=98.0, spread=abs(strategy.calculate_hedge_spread()))

    assert len(intents) == 1
    assert intents[0].purpose == "aggressive_down_heal_short"


def test_phase2_priority_blocks_phase3_and_phase4() -> None:
    strategy = build_strategy_for_update_state(
        long_size=6.0,
        long_avg=100.0,
        short_size=0.0,
        short_avg=95.0,
        realized_short=10.0,
    )
    strategy.config.enable_phase2_short_profit_long_reduce = True
    strategy.config.enable_phase3_long_rebuild = True
    strategy.config.enable_phase4_short_rebuild = True
    strategy._aggressive_down_heal_phase_completed = True
    strategy._phase3_long_target_reference_size = 10.0
    strategy._phase4_short_target_reference_size = 10.0

    intents = strategy.update_state(price=98.0, spread=abs(strategy.calculate_hedge_spread()))

    assert len(intents) == 1
    assert intents[0].purpose == "phase2_long_reduce_from_short_profit"


def test_phase2_must_block_phase3() -> None:
    strategy = build_strategy_for_update_state(
        long_size=6.0,
        long_avg=100.0,
        short_size=0.0,
        short_avg=95.0,
        realized_short=10.0,
    )
    strategy.config.enable_phase2_short_profit_long_reduce = True
    strategy.config.enable_phase3_long_rebuild = True
    strategy._aggressive_down_heal_phase_completed = True
    strategy._phase3_long_target_reference_size = 10.0

    intents = strategy.update_state(price=98.0, spread=abs(strategy.calculate_hedge_spread()))

    assert len(intents) == 1
    assert intents[0].purpose == "phase2_long_reduce_from_short_profit"


def test_phase3_requires_aggressive_phase_completion_when_enabled() -> None:
    strategy = build_strategy_for_update_state(
        long_size=6.0,
        long_avg=100.0,
        short_size=0.0,
        short_avg=96.0,
    )
    strategy.config.enable_aggressive_heal_phase = True
    strategy.config.enable_phase3_long_rebuild = True
    strategy._aggressive_down_heal_phase_completed = False
    strategy._phase3_long_target_reference_size = 10.0

    intents = strategy.update_state(price=97.5, spread=abs(strategy.calculate_hedge_spread()))

    assert all(intent.purpose != "phase3_long_rebuild" for intent in intents)


def test_phase3_priority_blocks_phase4_and_fine_heal() -> None:
    strategy = build_strategy_for_update_state(
        long_size=6.0,
        long_avg=100.0,
        short_size=0.0,
        short_avg=96.0,
    )
    strategy.config.enable_phase3_long_rebuild = True
    strategy.config.enable_phase4_short_rebuild = True
    strategy._phase3_long_target_reference_size = 10.0
    strategy._phase4_short_target_reference_size = 10.0

    intents = strategy.update_state(price=97.5, spread=abs(strategy.calculate_hedge_spread()))

    assert len(intents) == 1
    assert intents[0].purpose == "phase3_long_rebuild"


def test_phase4_priority_blocks_fine_heal() -> None:
    strategy = build_strategy_for_update_state(
        long_size=10.0,
        long_avg=108.0,
        short_size=6.0,
        short_avg=100.0,
    )
    strategy.config.enable_phase3_long_rebuild = True
    strategy.config.enable_phase4_short_rebuild = True
    strategy._phase3_long_target_reference_size = 10.0
    strategy._phase4_short_target_reference_size = 10.0

    intents = strategy.update_state(price=101.0, spread=abs(strategy.calculate_hedge_spread()))

    assert len(intents) == 1
    assert intents[0].purpose == "phase4_short_rebuild"


def test_fine_heal_runs_only_after_rebuild_targets_complete() -> None:
    strategy = build_strategy_for_update_state(
        long_size=10.0,
        long_avg=100.0,
        short_size=10.0,
        short_avg=96.0,
    )
    strategy.config.enable_phase3_long_rebuild = True
    strategy.config.enable_phase4_short_rebuild = True
    strategy._phase3_long_target_reference_size = 10.0
    strategy._phase4_short_target_reference_size = 10.0

    intents = strategy.update_state(price=97.5, spread=abs(strategy.calculate_hedge_spread()))

    assert len(intents) == 1
    assert intents[0].purpose == "spread_heal_short"
    assert intents[0].reduce_only is True

"""Fast unit tests for the backtest-only recovery/re-entry policy audit (B0..B5).

Pure decision helpers (``recovery_reentry_policy.py``) are tested directly against
lightweight fake ``BacktestResult``-like objects; the B5 staged inventory-MTM freeze
escalation is exercised against the same fake simulator harness used by
``test_inventory_mtm_freeze.py``. No real candle data or live config is touched.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from research.backtests.inventory_mtm_freeze import InventoryMtmFreezeConfig
from research.backtests.inventory_mtm_freeze_shim import install_inventory_mtm_freeze
from research.backtests.recovery_reentry_policy import (
    RECOVERY_VARIANTS,
    RecoveryReentryConfig,
    RecoveryReentryRuntimeState,
    apply_recovery_policy_after_trade,
    count_new_blockers_after_recovery,
    find_fresh_pullback_start_index,
    freeze_config_for_variant,
    is_fully_flat_result,
    is_target_blocker_first_flat,
    min_next_start_index,
    post_recovery_trade_pnl,
    previous_trade_is_clean_flat,
    resolve_cooldown_start_index,
    resolve_flat_mark_price,
    series_mtm_if_stopped_at_first_recovered_flat,
)
from research.backtests.test_inventory_mtm_freeze import _FakeSim, _fire_trigger


def _make_result(**overrides: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "trade_number": 1,
        "exit_reason": "flat_no_active_orders",
        "final_long_qty": 0.0,
        "final_short_qty": 0.0,
        "final_active_orders": [],
        "final_price": 100.0,
        "end_index": 10,
        "final_strategy_state_excerpt": {},
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _triggered_excerpt() -> dict[str, Any]:
    return {"inventory_mtm_trigger_event": {"trigger_candle": 1, "trigger_mtm": -5.0}}


class _FakeCandleClose:
    def __init__(self, close: float) -> None:
        self.close = close


def _candles(closes: list[float]) -> list[_FakeCandleClose]:
    return [_FakeCandleClose(c) for c in closes]


# ---------------------------------------------------------------------------
# Fully flat / clean-flat detection
# ---------------------------------------------------------------------------


def test_is_fully_flat_result_true_for_flat_no_active_orders() -> None:
    result = _make_result(exit_reason="flat_no_active_orders", final_long_qty=0.0, final_short_qty=0.0)
    assert is_fully_flat_result(result) is True


def test_is_fully_flat_result_true_for_recovery_joint_exit() -> None:
    result = _make_result(exit_reason="recovery_joint_exit", final_long_qty=1e-12, final_short_qty=-1e-12)
    assert is_fully_flat_result(result) is True


def test_is_fully_flat_result_false_for_open_series() -> None:
    result = _make_result(exit_reason="series_end_with_open_positions", final_long_qty=5.0)
    assert is_fully_flat_result(result) is False


def test_is_fully_flat_result_false_when_residual_qty() -> None:
    result = _make_result(exit_reason="flat_no_active_orders", final_long_qty=0.01)
    assert is_fully_flat_result(result) is False


def test_previous_trade_is_clean_flat_requires_strict_flat_and_no_orders() -> None:
    clean = _make_result(exit_reason="flat_no_active_orders", final_active_orders=[])
    assert previous_trade_is_clean_flat(clean) is True

    dirty_reason = _make_result(exit_reason="recovery_joint_exit")
    assert previous_trade_is_clean_flat(dirty_reason) is False

    dirty_orders = _make_result(exit_reason="flat_no_active_orders", final_active_orders=[{"purpose": "X"}])
    assert previous_trade_is_clean_flat(dirty_orders) is False

    dirty_qty = _make_result(exit_reason="flat_no_active_orders", final_short_qty=0.5)
    assert previous_trade_is_clean_flat(dirty_qty) is False


# ---------------------------------------------------------------------------
# Target blocker matching -- later blockers must never be attributed to the target.
# ---------------------------------------------------------------------------


def test_is_target_blocker_first_flat_requires_exact_trade_number_and_trigger() -> None:
    target = _make_result(
        trade_number=5, exit_reason="flat_no_active_orders", final_strategy_state_excerpt=_triggered_excerpt()
    )
    assert is_target_blocker_first_flat(
        result=target, target_blocker_trade_number=5, already_recovered=False
    ) is True


def test_is_target_blocker_first_flat_ignores_later_trade_numbers() -> None:
    later = _make_result(
        trade_number=6, exit_reason="flat_no_active_orders", final_strategy_state_excerpt=_triggered_excerpt()
    )
    assert is_target_blocker_first_flat(
        result=later, target_blocker_trade_number=5, already_recovered=False
    ) is False


def test_is_target_blocker_first_flat_requires_trigger_fired() -> None:
    no_trigger = _make_result(trade_number=5, exit_reason="flat_no_active_orders", final_strategy_state_excerpt={})
    assert is_target_blocker_first_flat(
        result=no_trigger, target_blocker_trade_number=5, already_recovered=False
    ) is False


def test_is_target_blocker_first_flat_false_once_already_recovered() -> None:
    target = _make_result(
        trade_number=5, exit_reason="flat_no_active_orders", final_strategy_state_excerpt=_triggered_excerpt()
    )
    assert is_target_blocker_first_flat(
        result=target, target_blocker_trade_number=5, already_recovered=True
    ) is False


def test_is_target_blocker_first_flat_never_matches_negative_target() -> None:
    result = _make_result(trade_number=1, final_strategy_state_excerpt=_triggered_excerpt())
    assert is_target_blocker_first_flat(
        result=result, target_blocker_trade_number=-1, already_recovered=False
    ) is False


# ---------------------------------------------------------------------------
# No same-candle reopen / cooldown skip helpers
# ---------------------------------------------------------------------------


def test_min_next_start_index_is_end_plus_one() -> None:
    assert min_next_start_index(41) == 42


def test_resolve_cooldown_start_index_passthrough_without_window() -> None:
    assert resolve_cooldown_start_index(candidate_start_index=10, cooldown_until_index=None) == 10


def test_resolve_cooldown_start_index_skips_inside_window() -> None:
    assert resolve_cooldown_start_index(candidate_start_index=10, cooldown_until_index=500) == 501


def test_resolve_cooldown_start_index_passthrough_past_window() -> None:
    assert resolve_cooldown_start_index(candidate_start_index=600, cooldown_until_index=500) == 600


# ---------------------------------------------------------------------------
# B3 fresh pullback signal scan
# ---------------------------------------------------------------------------


def test_find_fresh_pullback_start_index_accepts_first_qualifying_candle() -> None:
    candles = _candles([100.0, 99.8, 99.5, 99.4, 99.0])  # threshold @0.5% = 99.5
    found = find_fresh_pullback_start_index(
        candle_list=candles, from_index=0, flat_mark_price=100.0, fresh_pullback_pct=0.5
    )
    assert found == 2


def test_find_fresh_pullback_start_index_none_when_never_qualifies() -> None:
    candles = _candles([100.0, 99.9, 99.8])
    found = find_fresh_pullback_start_index(
        candle_list=candles, from_index=0, flat_mark_price=100.0, fresh_pullback_pct=0.5
    )
    assert found is None


def test_find_fresh_pullback_start_index_respects_from_index() -> None:
    candles = _candles([90.0, 90.0, 90.0])  # would qualify at index 0, but scan starts later
    found = find_fresh_pullback_start_index(
        candle_list=candles, from_index=2, flat_mark_price=100.0, fresh_pullback_pct=0.5
    )
    assert found == 2


def test_resolve_flat_mark_price_reads_absolute_candle_close() -> None:
    candles = _candles([1.0, 2.0, 3.0])
    assert resolve_flat_mark_price(candles, 1, fallback=999.0) == pytest.approx(2.0)


def test_resolve_flat_mark_price_falls_back_when_out_of_range() -> None:
    candles = _candles([1.0, 2.0])
    assert resolve_flat_mark_price(candles, 99, fallback=42.0) == pytest.approx(42.0)


# ---------------------------------------------------------------------------
# apply_recovery_policy_after_trade: per-variant behaviour
# ---------------------------------------------------------------------------


def test_config_rejects_unknown_variant() -> None:
    with pytest.raises(ValueError):
        RecoveryReentryConfig(variant="ZZ", target_blocker_trade_number=1)


def test_recovery_variants_tuple_is_b0_through_b5() -> None:
    assert RECOVERY_VARIANTS == ("B0", "B1", "B2", "B3", "B4", "B5")


def test_b0_leaves_default_next_start_untouched() -> None:
    config = RecoveryReentryConfig(variant="B0", target_blocker_trade_number=3)
    state = RecoveryReentryRuntimeState()
    result = _make_result(trade_number=3, final_strategy_state_excerpt=_triggered_excerpt(), end_index=10)

    outcome = apply_recovery_policy_after_trade(
        result=result, config=config, state=state, candle_list=[], default_next_start_index=11
    )

    assert outcome.should_break is False
    assert outcome.next_start_index == 11
    assert state.recovered is True
    assert result.final_strategy_state_excerpt["recovered_flat_of_target_blocker"] is True


def test_b1_stops_after_recovered_flat() -> None:
    config = RecoveryReentryConfig(variant="B1", target_blocker_trade_number=3)
    state = RecoveryReentryRuntimeState()
    result = _make_result(trade_number=3, final_strategy_state_excerpt=_triggered_excerpt(), end_index=10)

    outcome = apply_recovery_policy_after_trade(
        result=result, config=config, state=state, candle_list=[], default_next_start_index=11
    )

    assert outcome.should_break is True
    assert outcome.next_start_index is None
    assert result.final_strategy_state_excerpt["research_terminal_reason"] == "recovered_flat_terminal"

    # A later (non-target) trade must never re-trigger the branch again.
    later = _make_result(trade_number=4, final_strategy_state_excerpt=_triggered_excerpt(), end_index=20)
    outcome2 = apply_recovery_policy_after_trade(
        result=later, config=config, state=state, candle_list=[], default_next_start_index=21
    )
    assert outcome2.should_break is False
    assert later.final_strategy_state_excerpt["post_recovery_trade"] is True


def test_b2_skips_entries_during_cooldown_window() -> None:
    config = RecoveryReentryConfig(variant="B2", target_blocker_trade_number=3, cooldown_candles=500)
    state = RecoveryReentryRuntimeState()
    result = _make_result(trade_number=3, final_strategy_state_excerpt=_triggered_excerpt(), end_index=10)

    outcome = apply_recovery_policy_after_trade(
        result=result, config=config, state=state, candle_list=[], default_next_start_index=11
    )

    assert outcome.should_break is False
    assert state.cooldown_until_index == 510
    assert outcome.next_start_index == 511  # jumped past the cooldown window

    # A subsequent trade that would otherwise start inside the window is also skipped.
    next_result = _make_result(trade_number=4, end_index=520)
    outcome2 = apply_recovery_policy_after_trade(
        result=next_result, config=config, state=state, candle_list=[], default_next_start_index=200
    )
    assert outcome2.next_start_index == 511

    # And once past the window, the candidate index passes through untouched.
    later_result = _make_result(trade_number=5, end_index=520)
    outcome3 = apply_recovery_policy_after_trade(
        result=later_result, config=config, state=state, candle_list=[], default_next_start_index=600
    )
    assert outcome3.next_start_index == 600


def test_b3_only_accepts_pullback_fresh_signal_candle() -> None:
    config = RecoveryReentryConfig(variant="B3", target_blocker_trade_number=3, fresh_pullback_pct=0.5)
    state = RecoveryReentryRuntimeState()
    candles = _candles([100.0] * 11 + [99.9, 99.4, 99.0])  # index 10 = flat candle @100.0
    result = _make_result(
        trade_number=3,
        final_strategy_state_excerpt=_triggered_excerpt(),
        end_index=10,
        final_price=100.0,
    )

    outcome = apply_recovery_policy_after_trade(
        result=result, config=config, state=state, candle_list=candles, default_next_start_index=11
    )

    assert outcome.should_break is False
    assert outcome.next_start_index == 12  # first candle <= 99.5 after end_index
    assert result.final_strategy_state_excerpt["reentry_event"]["type"] == "fresh_signal_reentry"


def test_b3_breaks_when_no_fresh_signal_ever_arrives() -> None:
    config = RecoveryReentryConfig(variant="B3", target_blocker_trade_number=3, fresh_pullback_pct=0.5)
    state = RecoveryReentryRuntimeState()
    candles = _candles([100.0] * 11 + [99.9, 99.8])  # never drops far enough
    result = _make_result(
        trade_number=3,
        final_strategy_state_excerpt=_triggered_excerpt(),
        end_index=10,
        final_price=100.0,
    )

    outcome = apply_recovery_policy_after_trade(
        result=result, config=config, state=state, candle_list=candles, default_next_start_index=11
    )

    assert outcome.should_break is True
    assert outcome.next_start_index is None
    assert result.final_strategy_state_excerpt["research_terminal_reason"] == "no_fresh_pullback_signal"


def test_b4_rejects_reentry_if_not_clean_flat() -> None:
    config = RecoveryReentryConfig(variant="B4", target_blocker_trade_number=3)
    state = RecoveryReentryRuntimeState()
    result = _make_result(
        trade_number=3,
        exit_reason="recovery_joint_exit",  # not a strict flat_no_active_orders
        final_strategy_state_excerpt=_triggered_excerpt(),
        end_index=10,
    )

    outcome = apply_recovery_policy_after_trade(
        result=result, config=config, state=state, candle_list=[], default_next_start_index=11
    )

    assert outcome.should_break is True
    assert outcome.next_start_index is None
    assert result.final_strategy_state_excerpt["research_terminal_reason"] == "not_clean_flat_state"


def test_b4_allows_reentry_when_clean_flat() -> None:
    config = RecoveryReentryConfig(variant="B4", target_blocker_trade_number=3)
    state = RecoveryReentryRuntimeState()
    result = _make_result(
        trade_number=3,
        exit_reason="flat_no_active_orders",
        final_active_orders=[],
        final_strategy_state_excerpt=_triggered_excerpt(),
        end_index=10,
    )

    outcome = apply_recovery_policy_after_trade(
        result=result, config=config, state=state, candle_list=[], default_next_start_index=11
    )

    assert outcome.should_break is False
    assert outcome.next_start_index == 11
    assert result.final_strategy_state_excerpt["reentry_event"]["type"] == "state_reset_ok"


def test_b5_reentries_like_b0() -> None:
    config = RecoveryReentryConfig(variant="B5", target_blocker_trade_number=3)
    state = RecoveryReentryRuntimeState()
    result = _make_result(trade_number=3, final_strategy_state_excerpt=_triggered_excerpt(), end_index=10)

    outcome = apply_recovery_policy_after_trade(
        result=result, config=config, state=state, candle_list=[], default_next_start_index=11
    )

    assert outcome.should_break is False
    assert outcome.next_start_index == 11


def test_only_first_flat_of_target_ever_branches_never_target_means_normal_continuous() -> None:
    """If the target trade never flats, later trades behave exactly like plain continuous."""
    config = RecoveryReentryConfig(variant="B1", target_blocker_trade_number=3)
    state = RecoveryReentryRuntimeState()

    non_target_open = _make_result(trade_number=3, exit_reason="series_end_with_open_positions", final_long_qty=5.0)
    outcome = apply_recovery_policy_after_trade(
        result=non_target_open, config=config, state=state, candle_list=[], default_next_start_index=99
    )
    assert outcome.should_break is False
    assert outcome.next_start_index == 99
    assert state.recovered is False


# ---------------------------------------------------------------------------
# Metric helpers: series_mtm_if_stopped / post_recovery_trade_pnl / new blockers
# ---------------------------------------------------------------------------


def test_series_mtm_if_stopped_sums_up_to_and_including_target_when_recovered() -> None:
    rows = [
        {"trade_number": 1, "mtm_pnl": 1.0},
        {"trade_number": 2, "mtm_pnl": -2.0},
        {"trade_number": 3, "mtm_pnl": -5.0},  # target, recovered here
        {"trade_number": 4, "mtm_pnl": 100.0},  # post-recovery, excluded
    ]
    value = series_mtm_if_stopped_at_first_recovered_flat(
        trade_rows=rows, target_blocker_trade_number=3, recovered=True
    )
    assert value == pytest.approx(1.0 - 2.0 - 5.0)


def test_series_mtm_if_stopped_uses_full_series_when_never_recovered() -> None:
    rows = [{"trade_number": 1, "mtm_pnl": 1.0}, {"trade_number": 2, "mtm_pnl": -2.0}]
    value = series_mtm_if_stopped_at_first_recovered_flat(
        trade_rows=rows, target_blocker_trade_number=99, recovered=False
    )
    assert value == pytest.approx(-1.0)


def test_post_recovery_trade_pnl_is_the_delta() -> None:
    assert post_recovery_trade_pnl(series_mtm=-507.0, series_mtm_if_stopped=-47.0) == pytest.approx(-460.0)


def test_count_new_blockers_after_recovery_ignores_target_and_earlier() -> None:
    rows = [
        {"trade_number": 3, "is_blocker": 0},  # target itself, closed (recovered)
        {"trade_number": 4, "is_blocker": 1},  # new post-recovery blocker
        {"trade_number": 5, "is_blocker": 0},
        {"trade_number": 2, "is_blocker": 1},  # earlier row -- must never count
    ]
    count = count_new_blockers_after_recovery(trade_rows=rows, target_blocker_trade_number=3)
    assert count == 1


def test_later_blockers_not_attributed_to_original_target() -> None:
    """A blocker at trade_number > target must not satisfy is_target_blocker_first_flat."""
    later_blocker_flat = _make_result(
        trade_number=8, exit_reason="flat_no_active_orders", final_strategy_state_excerpt=_triggered_excerpt()
    )
    assert is_target_blocker_first_flat(
        result=later_blocker_flat, target_blocker_trade_number=3, already_recovered=False
    ) is False


# ---------------------------------------------------------------------------
# Freeze config pairing (B0 uses A1; B5 uses staged A2)
# ---------------------------------------------------------------------------


def test_freeze_config_for_b0_through_b4_is_plain_a1() -> None:
    for variant in ("B0", "B1", "B2", "B3", "B4"):
        config = freeze_config_for_variant(variant)
        assert config is not None
        assert config.variant == "A1"
        assert config.staged_cycle_freeze is False


def test_freeze_config_for_b5_is_staged_a2() -> None:
    config = freeze_config_for_variant("B5")
    assert config is not None
    assert config.variant == "A2"
    assert config.staged_cycle_freeze is True
    assert config.secondary_hold_candles_below_threshold == 100
    assert config.secondary_mtm_threshold_usdt == pytest.approx(-2.0)
    assert config.secondary_exit_increase_count == 2


# ---------------------------------------------------------------------------
# B5 staged secondary conditions escalate the exposure-only freeze to a cycle freeze.
# ---------------------------------------------------------------------------


def test_staged_freeze_escalates_via_mtm_below_secondary_threshold() -> None:
    sim = _FakeSim()
    install_inventory_mtm_freeze(
        sim,
        InventoryMtmFreezeConfig(
            variant="A2",
            staged_cycle_freeze=True,
            secondary_mtm_threshold_usdt=-2.0,
            secondary_hold_candles_below_threshold=100,
            secondary_exit_increase_count=2,
        ),
    )

    # Trigger fires at mtm=-1.5 (below -1, above -2): stage 1 only.
    _fire_trigger(sim, candle_index=1, mark=98.5, long_qty=1.0, short_qty=0.0)
    state = sim.strategy._backtest_inventory_mtm_freeze_state
    assert state.triggered is True
    assert state.cycle_freeze_enabled is False
    stage1_actions = [a for a in state.policy_actions if a["action"] == "stage1_exposure_freeze"]
    assert len(stage1_actions) == 1

    # A later candle drops mtm below -2 -> escalates to stage 2 (cycle freeze).
    sim.book.long_qty = 1.0
    sim.book.long_avg = 100.0
    sim.candle_index = 2
    sim.process_candle(_FakeCandleClose(97.5))  # mtm = -2.5

    assert state.cycle_freeze_enabled is True
    assert state.secondary_trigger_reason == "mtm_below_secondary_threshold"
    stage2_actions = [a for a in state.policy_actions if a["action"] == "stage2_cycle_freeze"]
    assert len(stage2_actions) == 1

    # Once escalated, new-cycle opens are blocked exactly like plain A1.
    from fixed_cycle_hedge_bot.models import StrategyIntent

    intent = StrategyIntent(side="long", qty=1.0, purpose="CYCLE_3_LONG_ADD")
    assert sim.intent_filter(intent) is False


def test_staged_freeze_escalates_via_hold_candles_below_threshold() -> None:
    sim = _FakeSim()
    install_inventory_mtm_freeze(
        sim,
        InventoryMtmFreezeConfig(
            variant="A2",
            staged_cycle_freeze=True,
            secondary_mtm_threshold_usdt=-100.0,  # unreachable in this test
            secondary_hold_candles_below_threshold=3,
            secondary_exit_increase_count=100,  # unreachable in this test
        ),
    )

    _fire_trigger(sim, candle_index=1, mark=98.5, long_qty=1.0, short_qty=0.0)  # mtm=-1.5, count=1
    state = sim.strategy._backtest_inventory_mtm_freeze_state
    assert state.candles_below_threshold_since_trigger == 1
    assert state.cycle_freeze_enabled is False

    sim.candle_index = 2
    sim.process_candle(_FakeCandleClose(98.5))  # still mtm=-1.5, count=2
    assert state.candles_below_threshold_since_trigger == 2
    assert state.cycle_freeze_enabled is False

    sim.candle_index = 3
    sim.process_candle(_FakeCandleClose(98.5))  # count=3 -> escalates
    assert state.cycle_freeze_enabled is True
    assert state.secondary_trigger_reason == "hold_candles_below_threshold"


def test_staged_freeze_hold_counter_resets_when_mtm_recovers_above_threshold() -> None:
    sim = _FakeSim()
    install_inventory_mtm_freeze(
        sim,
        InventoryMtmFreezeConfig(
            variant="A2",
            staged_cycle_freeze=True,
            secondary_mtm_threshold_usdt=-100.0,
            secondary_hold_candles_below_threshold=2,
            secondary_exit_increase_count=100,
        ),
    )

    _fire_trigger(sim, candle_index=1, mark=98.5, long_qty=1.0, short_qty=0.0)  # count=1
    state = sim.strategy._backtest_inventory_mtm_freeze_state

    sim.candle_index = 2
    sim.process_candle(_FakeCandleClose(101.0))  # mtm > -1 -> resets to 0
    assert state.candles_below_threshold_since_trigger == 0
    assert state.cycle_freeze_enabled is False

    sim.candle_index = 3
    sim.process_candle(_FakeCandleClose(98.5))  # count=1 again
    sim.candle_index = 4
    sim.process_candle(_FakeCandleClose(98.5))  # count=2 -> escalates now
    assert state.cycle_freeze_enabled is True


def test_staged_freeze_stage1_never_logs_block_new_cycle_before_escalation() -> None:
    """Stage 1 (A2 exposure freeze only) still blocks a new-cycle-open intent because it
    grows exposure -- but it must do so via ``block_exposure_growth``, never via the A1-style
    ``block_new_cycle`` path, until stage 2 (cycle freeze) has actually been enabled.
    """
    sim = _FakeSim()
    install_inventory_mtm_freeze(
        sim,
        InventoryMtmFreezeConfig(
            variant="A2",
            staged_cycle_freeze=True,
            secondary_mtm_threshold_usdt=-100.0,
            secondary_hold_candles_below_threshold=1000,
            secondary_exit_increase_count=1000,
        ),
    )
    _fire_trigger(sim, candle_index=1, mark=98.5, long_qty=1.0, short_qty=0.0)

    from fixed_cycle_hedge_bot.models import StrategyIntent

    cycle_intent = StrategyIntent(side="long", qty=1.0, purpose="CYCLE_3_LONG_ADD")
    assert sim.intent_filter(cycle_intent) is False  # blocked, but only via exposure-growth path

    state = sim.strategy._backtest_inventory_mtm_freeze_state
    assert state.cycle_freeze_enabled is False
    assert not any(a["action"] == "block_new_cycle" for a in state.policy_actions)
    assert any(a["action"] == "block_exposure_growth" for a in state.policy_actions)

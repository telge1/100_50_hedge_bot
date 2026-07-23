"""Unit tests for blocker recovery trigger/hybrid research policies."""

from __future__ import annotations

from dataclasses import replace

import pytest

from research.backtests.blocker_recovery_trigger_policy import (
    PRIOR_B1_RECOVERED_COINS,
    PRIOR_B1_UNRECOVERED_COINS,
    build_c0_c4_specs,
    build_c5_specs,
    group_feature_stats,
    pick_best_c0_c4_candidate,
    quantile,
    rank_separating_features,
    terminal_recovery_config,
)
from research.backtests.inventory_mtm_freeze import (
    InventoryMtmFreezeConfig,
    evaluate_primary_trigger,
    required_recovery_move_pct,
)
from research.backtests.inventory_mtm_freeze_shim import install_inventory_mtm_freeze
from research.backtests.test_inventory_mtm_freeze import _FakeSim, _fire_trigger


def test_prior_cohorts_partition_27() -> None:
    assert len(PRIOR_B1_RECOVERED_COINS) == 12
    assert len(PRIOR_B1_UNRECOVERED_COINS) == 15
    assert PRIOR_B1_RECOVERED_COINS.isdisjoint(PRIOR_B1_UNRECOVERED_COINS)
    assert len(PRIOR_B1_RECOVERED_COINS | PRIOR_B1_UNRECOVERED_COINS) == 27


def test_c0_config_matches_classic_a1() -> None:
    specs = {s.name: s for s in build_c0_c4_specs()}
    c0 = specs["C0"].freeze_config
    assert c0.variant == "A1"
    assert c0.threshold_usdt == -1.0
    assert c0.use_mtm_trigger is True
    assert c0.use_cycle_trigger is False
    assert c0.staged_cycle_freeze is False
    assert c0.emergency_neutralize_after_candles is None
    assert terminal_recovery_config(target_blocker_trade_number=3).variant == "B1"


def test_evaluate_primary_trigger_mtm_and_cycle_and() -> None:
    cfg = InventoryMtmFreezeConfig(
        variant="A1",
        threshold_usdt=-0.75,
        use_mtm_trigger=True,
        use_cycle_trigger=True,
        cycle_count_threshold=2,
        trigger_combine="and",
    )
    fire, details = evaluate_primary_trigger(
        config=cfg, mtm=-0.8, cycle_count=1, exit_increase_count=0, required_recovery_move=None
    )
    assert fire is False
    fire, details = evaluate_primary_trigger(
        config=cfg, mtm=-0.8, cycle_count=2, exit_increase_count=0, required_recovery_move=None
    )
    assert fire is True
    assert details["mtm_ok"] is True
    assert details["cycle_ok"] is True


def test_evaluate_primary_trigger_or() -> None:
    cfg = InventoryMtmFreezeConfig(
        variant="A1",
        threshold_usdt=-0.75,
        use_mtm_trigger=True,
        use_cycle_trigger=True,
        cycle_count_threshold=3,
        trigger_combine="or",
    )
    fire, _ = evaluate_primary_trigger(
        config=cfg, mtm=0.0, cycle_count=3, exit_increase_count=0, required_recovery_move=None
    )
    assert fire is True
    fire, _ = evaluate_primary_trigger(
        config=cfg, mtm=-0.8, cycle_count=1, exit_increase_count=0, required_recovery_move=None
    )
    assert fire is True


def test_required_recovery_move_pct_long() -> None:
    assert required_recovery_move_pct(mark=100.0, active_exit=101.0, primary_side="long") == pytest.approx(1.0)
    assert required_recovery_move_pct(mark=100.0, active_exit=None, primary_side="long") is None


def test_cycle_trigger_fires_without_mtm() -> None:
    sim = _FakeSim()
    sim.runtime_state.strategy_state = {"active_cycle_index": 2, "completed_cycle_count": 2}
    install_inventory_mtm_freeze(
        sim,
        InventoryMtmFreezeConfig(
            variant="A1",
            use_mtm_trigger=False,
            use_cycle_trigger=True,
            cycle_count_threshold=2,
        ),
    )
    _fire_trigger(sim, candle_index=1, mark=100.5, long_qty=10.0, short_qty=0.0)
    assert sim.strategy._backtest_inventory_mtm_trigger_event is not None
    assert sim.strategy._backtest_inventory_mtm_freeze_state.cycle_freeze_enabled is True


def test_mtm_threshold_first_causal_candle() -> None:
    sim = _FakeSim()
    install_inventory_mtm_freeze(sim, InventoryMtmFreezeConfig(variant="A1", threshold_usdt=-0.5))
    _fire_trigger(sim, candle_index=3, mark=99.94, long_qty=10.0, short_qty=0.0)
    event = sim.strategy._backtest_inventory_mtm_trigger_event
    assert event is not None
    assert event["trigger_candle"] == 3
    assert event["trigger_mtm"] == pytest.approx(-0.6)


def test_c4_stage_order_a2_then_a1() -> None:
    sim = _FakeSim()
    install_inventory_mtm_freeze(
        sim,
        InventoryMtmFreezeConfig(
            variant="A2",
            threshold_usdt=-0.5,
            staged_cycle_freeze=True,
            secondary_use_hold=False,
            secondary_use_mtm=True,
            secondary_use_exit_increase=False,
            secondary_mtm_threshold_usdt=-1.0,
        ),
    )
    _fire_trigger(sim, candle_index=1, mark=99.94, long_qty=10.0, short_qty=0.0)
    state = sim.strategy._backtest_inventory_mtm_freeze_state
    assert state.triggered is True
    assert state.cycle_freeze_enabled is False
    actions = [a["action"] for a in state.policy_actions]
    assert "stage1_exposure_freeze" in actions

    _fire_trigger(sim, candle_index=2, mark=99.8, long_qty=10.0, short_qty=0.0)
    assert state.cycle_freeze_enabled is True
    assert state.secondary_trigger_reason == "mtm_below_secondary_threshold"


def test_a2_blocks_exposure_growth_only() -> None:
    from fixed_cycle_hedge_bot.models import StrategyIntent

    sim = _FakeSim()
    install_inventory_mtm_freeze(sim, InventoryMtmFreezeConfig(variant="A2", threshold_usdt=-0.5))
    # mtm = 10*(99.8-100)+5*(100-99.8) = -2+1 = -1 < -0.5
    _fire_trigger(sim, candle_index=1, mark=99.8, long_qty=10.0, short_qty=5.0)
    assert sim.strategy._backtest_inventory_mtm_trigger_event is not None
    assert sim.intent_filter(
        StrategyIntent(side="long", qty=1.0, purpose="LONG_TP_EXIT", order_type="Market", reduce_only=True)
    )
    assert not sim.intent_filter(
        StrategyIntent(side="long", qty=1.0, purpose="CYCLE_2_LONG_ADD", order_type="Limit", reduce_only=False)
    )


def test_a1_blocks_new_cycles_only() -> None:
    from fixed_cycle_hedge_bot.models import StrategyIntent

    sim = _FakeSim()
    install_inventory_mtm_freeze(sim, InventoryMtmFreezeConfig(variant="A1"))
    _fire_trigger(sim, candle_index=1, mark=99.0, long_qty=10.0, short_qty=5.0)
    assert not sim.intent_filter(
        StrategyIntent(side="long", qty=1.0, purpose="CYCLE_2_LONG_ADD", order_type="Limit", reduce_only=False)
    )
    assert sim.intent_filter(
        StrategyIntent(side="long", qty=1.0, purpose="MANUAL_ADD", order_type="Limit", reduce_only=False)
    )


def test_emergency_window_and_once_flag() -> None:
    """Emergency arms after the window; once fired it stays latched."""
    sim = _FakeSim()
    install_inventory_mtm_freeze(
        sim,
        InventoryMtmFreezeConfig(
            variant="A1",
            threshold_usdt=-0.5,
            emergency_neutralize_after_candles=2,
            emergency_neutralize_fraction=0.25,
        ),
    )
    state = sim.strategy._backtest_inventory_mtm_freeze_state
    assert state.emergency_armed is True

    _fire_trigger(sim, candle_index=1, mark=99.9, long_qty=10.0, short_qty=0.0)
    assert state.triggered is True
    assert state.emergency_fired is False

    _fire_trigger(sim, candle_index=2, mark=99.9, long_qty=10.0, short_qty=0.0)
    assert state.emergency_fired is False

    state.emergency_fired = True
    state.force_exposure_freeze_after_emergency = True
    state.cycle_freeze_enabled = True
    _fire_trigger(sim, candle_index=5, mark=99.9, long_qty=7.5, short_qty=0.0)
    assert state.emergency_fired is True
    assert state.force_exposure_freeze_after_emergency is True


def test_terminal_recovery_config_is_b1() -> None:
    cfg = terminal_recovery_config(target_blocker_trade_number=7)
    assert cfg.variant == "B1"
    assert cfg.target_blocker_trade_number == 7


def test_c5_builds_four_from_base() -> None:
    base = next(s for s in build_c0_c4_specs() if s.name == "C0")
    c5 = build_c5_specs(base)
    assert [s.name for s in c5] == ["C5a", "C5b", "C5c", "C5d"]
    assert c5[0].freeze_config.emergency_neutralize_after_candles == 250
    assert c5[0].freeze_config.emergency_neutralize_fraction == 0.25
    assert c5[3].freeze_config.emergency_neutralize_after_candles == 500
    assert c5[3].freeze_config.emergency_neutralize_fraction == 0.50
    assert c5[3].freeze_config.threshold_usdt == base.freeze_config.threshold_usdt


def test_pick_best_prefers_series_mtm() -> None:
    rows = [
        {"variant": "C0", "series_mtm_terminal_stop": -168.0, "recovery_rate": 0.44},
        {"variant": "C1a", "series_mtm_terminal_stop": -100.0, "recovery_rate": 0.30},
        {"variant": "C2a", "series_mtm_terminal_stop": -100.0, "recovery_rate": 0.50},
    ]
    assert pick_best_c0_c4_candidate(rows) == "C2a"


def test_feature_stats_and_rank() -> None:
    recovered = [{"trigger_candle": 10.0, "final_mtm": 1.0}, {"trigger_candle": 20.0, "final_mtm": 2.0}]
    unrecovered = [{"trigger_candle": 100.0, "final_mtm": -10.0}, {"trigger_candle": 200.0, "final_mtm": -20.0}]
    stats = group_feature_stats(recovered, feature="trigger_candle")
    assert stats["n"] == 2
    assert stats["median"] == pytest.approx(15.0)
    ranked = rank_separating_features(recovered, unrecovered, features=("trigger_candle", "final_mtm"))
    assert ranked[0]["feature"] in {"trigger_candle", "final_mtm"}
    assert quantile([1, 2, 3, 4], 0.5) == pytest.approx(2.5)


def test_combined_required_recovery_move_gate() -> None:
    cfg = InventoryMtmFreezeConfig(
        variant="A1",
        threshold_usdt=-0.75,
        use_mtm_trigger=True,
        use_required_recovery_move_trigger=True,
        required_recovery_move_pct_threshold=1.0,
        trigger_combine="and",
    )
    fire, _ = evaluate_primary_trigger(
        config=cfg, mtm=-0.8, cycle_count=1, exit_increase_count=0, required_recovery_move=0.5
    )
    assert fire is False
    fire, _ = evaluate_primary_trigger(
        config=cfg, mtm=-0.8, cycle_count=1, exit_increase_count=0, required_recovery_move=1.5
    )
    assert fire is True


def test_c0_spec_replace_preserves_threshold() -> None:
    base = next(s for s in build_c0_c4_specs() if s.name == "C0")
    emergency = replace(base.freeze_config, emergency_neutralize_after_candles=250)
    assert emergency.threshold_usdt == -1.0
    assert emergency.emergency_neutralize_after_candles == 250

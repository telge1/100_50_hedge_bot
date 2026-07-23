"""Unit tests for fixed absolute %-step distance staging."""

from __future__ import annotations

import pytest

from research.backtests.fixed_step_distance_staging import (
    absolute_distances_to_price_fractions,
    backloaded_qty_fractions,
    build_fixed_step_percentages,
    config_with_fixed_step_plan,
    equal_qty_fractions,
    fixed_step_base_config,
    is_fixed_step_profile,
    select_fixed_step_plan,
)
from research.backtests.second_leg_price_staging import (
    build_stage_plan,
    resolve_grid_profile,
    validate_config,
)


@pytest.mark.parametrize(
    "d,step,expected",
    [
        (0.5, 1.0, (0.5,)),
        (1.0, 1.0, (1.0,)),
        (1.5, 1.0, (1.0, 1.5)),
        (2.0, 1.0, (1.0, 2.0)),
        (2.1, 1.0, (1.0, 2.0, 2.1)),
        (4.0, 1.0, (1.0, 2.0, 3.0, 4.0)),
        (5.5, 1.0, (1.0, 2.0, 3.0, 4.0, 5.0, 5.5)),
        (8.0, 1.0, (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0)),
        (1.5, 2.0, (1.5,)),
        (2.0, 2.0, (2.0,)),
        (3.0, 2.0, (2.0, 3.0)),
        (4.0, 2.0, (2.0, 4.0)),
        (5.5, 2.0, (2.0, 4.0, 5.5)),
        (7.1, 2.0, (2.0, 4.0, 6.0, 7.1)),
        (8.0, 2.0, (2.0, 4.0, 6.0, 8.0)),
        (8.1, 2.0, (2.0, 4.0, 6.0, 8.0, 8.1)),
        (10.0, 2.0, (2.0, 4.0, 6.0, 8.0, 10.0)),
    ],
)
def test_build_fixed_step_percentages_examples(d, step, expected) -> None:
    got = build_fixed_step_percentages(d, step, max_stages=20)
    assert len(got) == len(expected)
    for a, b in zip(got, expected):
        assert a == pytest.approx(b, abs=1e-9)
    assert got[-1] == pytest.approx(d, abs=1e-12)


def test_cap_keeps_earliest_plus_full() -> None:
    # 13.12% @ 1% → 14 uncapped; max_stages=8 → 1..7 + 13.12
    got = build_fixed_step_percentages(13.12, 1.0, max_stages=8)
    assert len(got) == 8
    assert got[:7] == pytest.approx((1, 2, 3, 4, 5, 6, 7), abs=1e-9)
    assert got[-1] == pytest.approx(13.12, abs=1e-12)


@pytest.mark.parametrize(
    "d,step,expected_fracs",
    [
        (2.0, 1.0, (0.5, 1.0)),
        (4.0, 1.0, (0.25, 0.5, 0.75, 1.0)),
        (4.0, 2.0, (0.5, 1.0)),
        (5.5, 2.0, (2 / 5.5, 4 / 5.5, 1.0)),
        (8.0, 2.0, (0.25, 0.5, 0.75, 1.0)),
    ],
)
def test_price_fractions_from_grid(d, step, expected_fracs) -> None:
    abs_d = build_fixed_step_percentages(d, step, max_stages=20)
    fracs = absolute_distances_to_price_fractions(abs_d, d)
    assert len(fracs) == len(expected_fracs)
    for a, b in zip(fracs, expected_fracs):
        assert a == pytest.approx(b, abs=1e-12)
    assert fracs[-1] == 1.0
    assert all(fracs[i] < fracs[i + 1] for i in range(len(fracs) - 1))


def test_single_stage_plans_are_legacy_fallback_candidates() -> None:
    for d, step in ((0.5, 1.0), (1.0, 1.0), (1.5, 2.0), (2.0, 2.0)):
        plan = select_fixed_step_plan("fixed_step_1pct_equal" if step == 1 else "fixed_step_2pct_equal", d)
        assert plan is not None
        assert plan.stage_count == 1


def test_equal_and_backloaded_qty_sum_to_one() -> None:
    for n in (2, 3, 4, 6, 8):
        eq = equal_qty_fractions(n)
        bl = backloaded_qty_fractions(n)
        assert sum(eq) == pytest.approx(1.0)
        assert sum(bl) == pytest.approx(1.0)
        assert bl[0] <= bl[-1]
        assert all(x > 0 for x in eq + bl)


def test_profiles_resolve_and_validate() -> None:
    for name in (
        "fixed_step_1pct_equal",
        "fixed_step_2pct_equal",
        "fixed_step_2pct_backloaded",
        "fixed_step_1pct_backloaded",
    ):
        assert is_fixed_step_profile(name)
        cfg = resolve_grid_profile(name)
        assert cfg.enabled and cfg.fixed_step and cfg.only_cycles == (4,)
        assert validate_config(cfg) == []
    # unchanged baselines
    assert resolve_grid_profile("two_early_medium").adaptive is False
    assert resolve_grid_profile("two_early_medium").fixed_step is False
    assert resolve_grid_profile("adaptive_equal").adaptive is True
    assert resolve_grid_profile("adaptive_equal").fixed_step is False


@pytest.mark.parametrize(
    "profile,d",
    [
        ("fixed_step_1pct_equal", 5.5),
        ("fixed_step_1pct_equal", 8.0),
        ("fixed_step_2pct_equal", 5.5),
        ("fixed_step_2pct_equal", 8.0),
        ("fixed_step_2pct_backloaded", 8.0),
        ("fixed_step_2pct_backloaded", 10.0),
        ("fixed_step_1pct_equal", 13.12),
    ],
)
def test_planner_accepts_fixed_step_plans(profile: str, d: float) -> None:
    fs = select_fixed_step_plan(profile, d)
    assert fs is not None and fs.stage_count >= 2
    base = fixed_step_base_config(profile)
    cfg = config_with_fixed_step_plan(base, fs)
    first, full = 100.0, 100.0 * (1.0 - d / 100.0)
    plan = build_stage_plan(
        config=cfg,
        cycle_index=4,
        purpose="CYCLE_4_SHORT_REDUCE",
        first_leg_fill_price=first,
        full_trigger_price=full,
        total_qty=40.0,
        required_net=max(1.0, 40.0 * abs(first - full) * 0.15),
        short_entry_price=first * 1.01,
        fee_rate=0.00055,
        price_tick=0.0001,
        qty_step=0.01,
        min_order_qty=0.01,
    )
    assert plan.accepted
    assert plan.stage_count >= 2
    assert abs(sum(s.qty for s in plan.stages) - 40.0) <= 0.02
    prices = [s.trigger_price for s in plan.stages]
    assert prices == sorted(prices, reverse=True)
    assert abs(prices[-1] - full) <= 0.0002 + 1e-9
    assert plan.stages[-1].price_fraction == pytest.approx(1.0)


def test_min_notional_reduce_preserves_distance_diag() -> None:
    fs = select_fixed_step_plan("fixed_step_1pct_equal", 8.0)
    assert fs is not None
    assert fs.requested_stage_count == 8
    base = fixed_step_base_config("fixed_step_1pct_equal")
    cfg = config_with_fixed_step_plan(base, fs)
    plan = build_stage_plan(
        config=cfg,
        cycle_index=4,
        purpose="CYCLE_4_SHORT_REDUCE",
        first_leg_fill_price=100.0,
        full_trigger_price=92.0,
        total_qty=2.5,
        required_net=0.3,
        short_entry_price=101.0,
        fee_rate=0.00055,
        price_tick=0.0001,
        qty_step=0.01,
        min_order_qty=0.01,
    )
    assert plan.accepted
    assert plan.stage_count <= fs.stage_count
    if plan.stage_count < fs.stage_count:
        assert plan.fallback_used == "reduce_stage_count"


def test_cap_flag_on_large_distance() -> None:
    fs = select_fixed_step_plan("fixed_step_1pct_equal", 13.12, max_stages=8)
    assert fs is not None
    assert fs.stage_cap_applied is True
    assert fs.capped_stage_count == 8
    assert fs.requested_stage_count > 8
    assert fs.price_fractions[-1] == 1.0

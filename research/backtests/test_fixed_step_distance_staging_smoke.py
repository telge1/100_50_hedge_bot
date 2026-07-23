"""Synthetic economics smoke for fixed-step distance staging."""

from __future__ import annotations

import pytest

from research.backtests.fixed_step_distance_staging import (
    config_with_fixed_step_plan,
    fixed_step_base_config,
    select_fixed_step_plan,
)
from research.backtests.second_leg_price_staging import build_stage_plan


PROFILES = (
    "fixed_step_1pct_equal",
    "fixed_step_2pct_equal",
    "fixed_step_2pct_backloaded",
)
DISTANCES = (2.0, 4.0, 5.5, 8.0, 10.0)


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("d", DISTANCES)
def test_synthetic_fixed_step_economics_smoke(profile: str, d: float) -> None:
    fs = select_fixed_step_plan(profile, d)
    assert fs is not None
    if fs.stage_count <= 1:
        # Small distances vs large step → legacy fallback path
        return
    cfg = config_with_fixed_step_plan(fixed_step_base_config(profile), fs)
    first = 100.0
    full = first * (1.0 - d / 100.0)
    plan = build_stage_plan(
        config=cfg,
        cycle_index=4,
        purpose="CYCLE_4_SHORT_REDUCE",
        first_leg_fill_price=first,
        full_trigger_price=full,
        total_qty=50.0,
        required_net=max(2.0, 50.0 * abs(first - full) * 0.12),
        short_entry_price=first * 1.01,
        fee_rate=0.00055,
        price_tick=0.0001,
        qty_step=0.01,
        min_order_qty=0.01,
    )
    assert plan.accepted
    assert plan.stage_count >= 2
    assert abs(sum(s.qty for s in plan.stages) - 50.0) <= 0.02
    # Residual coverage: last stage non-zero
    assert plan.stages[-1].qty > 0
    # Monotone prices toward full trigger
    prices = [s.trigger_price for s in plan.stages]
    assert prices == sorted(prices, reverse=True)
    assert abs(prices[-1] - full) <= 0.0002 + 1e-9
    # No duplicate triggers after tick rounding
    assert len(set(round(p, 6) for p in prices)) == len(prices)


def test_1pct_backloaded_early_shares_not_tiny() -> None:
    fs = select_fixed_step_plan("fixed_step_1pct_backloaded", 8.0)
    assert fs is not None and fs.stage_count == 8
    # With floor, earliest share should not collapse below ~3%
    assert fs.qty_fractions[0] >= 0.03
    # Still backloaded
    assert fs.qty_fractions[0] < fs.qty_fractions[-1]

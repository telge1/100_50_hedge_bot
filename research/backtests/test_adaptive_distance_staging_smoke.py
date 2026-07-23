"""Stufe B micro-smoke: synthetic bucket fixtures + planner/shim guards."""

from __future__ import annotations

import pytest

from research.backtests.adaptive_distance_staging import (
    DistanceBucket,
    adaptive_base_config,
    classify_distance_status,
    compute_original_distance_pct,
    config_with_adaptive_policy,
    select_adaptive_policy,
    select_distance_bucket,
)
from research.backtests.second_leg_price_staging import build_stage_plan, resolve_grid_profile


def _plan_for_distance(profile: str, first: float, full: float, *, qty: float = 40.0):
    d = compute_original_distance_pct(first, full)
    bucket = select_distance_bucket(d)
    policy = select_adaptive_policy(profile, d)
    base = adaptive_base_config(profile)
    if policy is None:
        return d, bucket, None, None
    cfg = config_with_adaptive_policy(base, policy)
    plan = build_stage_plan(
        config=cfg,
        cycle_index=4,
        purpose="CYCLE_4_SHORT_REDUCE",
        first_leg_fill_price=first,
        full_trigger_price=full,
        total_qty=qty,
        required_net=max(1.0, qty * abs(first - full) * 0.2),
        short_entry_price=first * 1.01,
        fee_rate=0.00055,
        price_tick=0.0001,
        qty_step=0.01,
        min_order_qty=0.01,
    )
    return d, bucket, policy, plan


# (distance_pct, expected_bucket)
_BOUNDARY_CASES = [
    (0.5, DistanceBucket.D_0_2),
    (2.0, DistanceBucket.D_0_2),
    (2.1, DistanceBucket.D_2_4),
    (4.0, DistanceBucket.D_2_4),
    (4.1, DistanceBucket.D_4_7),
    (7.0, DistanceBucket.D_4_7),
    (7.1, DistanceBucket.D_GT_7),
    (10.0, DistanceBucket.D_GT_7),
]


def _first_full_for_distance(d_pct: float) -> tuple[float, float]:
    """Build prices with exact percent distance (avoid binary float edge drift)."""
    first = 100.0
    # Solve abs(full-first)/first*100 = d_pct with full < first
    full = first - (d_pct / 100.0) * first
    # Nudge inward for exact boundary edges that must land in the closed upper bucket.
    recon = compute_original_distance_pct(first, full)
    if recon is not None and d_pct in (2.0, 4.0, 7.0) and recon > d_pct:
        full = first - ((d_pct - 1e-12) / 100.0) * first
    return first, full


@pytest.mark.parametrize("profile", ["adaptive_equal", "adaptive_backloaded"])
@pytest.mark.parametrize("d_pct,expected", _BOUNDARY_CASES)
def test_synthetic_boundary_bucket_fixtures(profile: str, d_pct: float, expected: DistanceBucket) -> None:
    first, full = _first_full_for_distance(d_pct)
    d, bucket, policy, plan = _plan_for_distance(profile, first, full)
    assert d == pytest.approx(d_pct, rel=0, abs=1e-9)
    assert bucket == expected
    assert policy is not None
    assert policy.bucket == expected
    assert plan is not None and plan.accepted
    assert plan.stage_count == policy.stage_count or plan.fallback_used == "reduce_stage_count"
    assert list(policy.price_fractions) == list(
        select_adaptive_policy(profile, d_pct).price_fractions  # type: ignore[union-attr]
    )
    # Residual coverage: qty sums to total
    assert abs(sum(s.qty for s in plan.stages) - 40.0) <= 0.02
    # Prices monotone toward full trigger (long-primary: decreasing)
    prices = [s.trigger_price for s in plan.stages]
    assert prices == sorted(prices, reverse=True)
    assert abs(prices[-1] - full) <= 0.0002 + 1e-9
    # Eligibility: only_cycles=(4,) in base config
    assert adaptive_base_config(profile).only_cycles == (4,)


@pytest.mark.parametrize("profile", ["adaptive_equal", "adaptive_backloaded"])
def test_bucket_preserved_on_min_notional_fallback(profile: str) -> None:
    first, full = _first_full_for_distance(10.0)
    d, bucket, policy, plan = _plan_for_distance(profile, first, full, qty=2.5)
    assert bucket == DistanceBucket.D_GT_7
    assert policy is not None
    assert plan is not None and plan.accepted
    assert plan.stage_count <= policy.stage_count
    if plan.stage_count < policy.stage_count:
        assert plan.fallback_used == "reduce_stage_count"
    # Diagnostics must still know the original bucket even if effective stages → 1
    status = classify_distance_status(
        profile=profile,
        max_cycle=4,
        distance_pct=d,
        bucket=bucket,
        has_c4_followup_plan=True,
        plan_accepted=True,
        adaptive=True,
    )
    assert status == "gt_7"


def test_synthetic_bucket_fixtures_equal_legacy_smoke() -> None:
    cases = [
        (100.0, 99.0, DistanceBucket.D_0_2),
        (100.0, 97.0, DistanceBucket.D_2_4),
        (100.0, 94.5, DistanceBucket.D_4_7),
        (100.0, 90.0, DistanceBucket.D_GT_7),
    ]
    for first, full, expected in cases:
        d, bucket, policy, plan = _plan_for_distance("adaptive_equal", first, full)
        assert bucket == expected
        assert policy is not None
        assert plan is not None and plan.accepted
        assert plan.stage_count >= 2
        assert abs(sum(s.qty for s in plan.stages) - 40.0) <= 0.02


def test_synthetic_bucket_fixtures_backloaded() -> None:
    d, bucket, policy, plan = _plan_for_distance("adaptive_backloaded", 50.0, 46.0)  # 8%
    assert bucket == DistanceBucket.D_GT_7
    assert policy is not None
    assert policy.qty_fractions[0] == 0.15
    assert plan is not None and plan.accepted


def test_min_notional_fallback_fixture() -> None:
    d, bucket, policy, plan = _plan_for_distance(
        "adaptive_equal", 100.0, 90.0, qty=2.5
    )
    assert policy is not None
    assert plan is not None and plan.accepted
    assert plan.stage_count <= policy.stage_count
    if plan.stage_count < policy.stage_count:
        assert plan.fallback_used == "reduce_stage_count"


def test_non_positive_falls_back() -> None:
    d = compute_original_distance_pct(100.0, 100.0)
    assert d == 0.0
    assert select_adaptive_policy("adaptive_equal", d) is None
    assert (
        classify_distance_status(
            profile="adaptive_equal",
            max_cycle=4,
            distance_pct=d,
            bucket=DistanceBucket.NON_POSITIVE,
            has_c4_followup_plan=True,
            plan_accepted=False,
            adaptive=True,
        )
        == "non_positive_distance"
    )


def test_classify_unknown_replacements() -> None:
    assert (
        classify_distance_status(
            profile="adaptive_equal",
            max_cycle=2,
            distance_pct=None,
            bucket=None,
            has_c4_followup_plan=False,
            adaptive=True,
        )
        == "not_applicable_before_cycle4"
    )
    assert (
        classify_distance_status(
            profile="adaptive_equal",
            max_cycle=4,
            distance_pct=None,
            bucket=None,
            has_c4_followup_plan=False,
            adaptive=True,
        )
        == "cycle4_pending_no_followup"
    )
    assert (
        classify_distance_status(
            profile="two_early_medium",
            max_cycle=4,
            distance_pct=None,
            bucket=None,
            has_c4_followup_plan=False,
            adaptive=False,
        )
        == "fixed_profile_no_adaptive_bucket"
    )


def test_tem_parity_fractions_still_fixed() -> None:
    cfg = resolve_grid_profile("two_early_medium")
    plan = build_stage_plan(
        config=cfg,
        cycle_index=4,
        purpose="CYCLE_4_SHORT_REDUCE",
        first_leg_fill_price=100.0,
        full_trigger_price=90.0,
        total_qty=40.0,
        required_net=2.0,
        short_entry_price=101.0,
        fee_rate=0.00055,
        price_tick=0.0001,
        qty_step=0.01,
        min_order_qty=0.01,
    )
    assert plan.accepted
    assert plan.stage_count == 2
    assert [s.price_fraction for s in plan.stages] == [0.40, 1.00]
    assert cfg.adaptive is False


def test_cycle_filter_only_cycle4_in_config() -> None:
    cfg = resolve_grid_profile("adaptive_equal")
    assert cfg.only_cycles == (4,)

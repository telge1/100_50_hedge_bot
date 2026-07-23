"""Stufe A unit tests for adaptive distance staging."""

from __future__ import annotations

from pathlib import Path

import pytest

from research.backtests.adaptive_distance_staging import (
    DistanceBucket,
    adaptive_base_config,
    classify_distance_status,
    compute_original_distance_pct,
    config_with_adaptive_policy,
    is_adaptive_profile,
    select_adaptive_policy,
    select_distance_bucket,
)
from research.backtests.second_leg_price_staging import (
    build_stage_plan,
    price_at_fraction,
    resolve_grid_profile,
    resolve_profile,
    validate_config,
)
from research.backtests.second_leg_price_staging_shim import install_second_leg_price_staging
from research.backtests.backtest_config_loader import resolve_backtest_config
from research.backtests.hedge_bot_original_simulator import build_strategy
from research.backtests.two_early_medium_window_plan import (
    window_pair_key,
    window_profile_run_key,
)


@pytest.mark.parametrize(
    "d,expected",
    [
        (2.0, DistanceBucket.D_0_2),
        (2.0001, DistanceBucket.D_2_4),
        (4.0, DistanceBucket.D_2_4),
        (4.0001, DistanceBucket.D_4_7),
        (7.0, DistanceBucket.D_4_7),
        (7.0001, DistanceBucket.D_GT_7),
        (0.0, DistanceBucket.NON_POSITIVE),
        (-1.0, DistanceBucket.NON_POSITIVE),
        (None, DistanceBucket.INVALID),
        (float("nan"), DistanceBucket.INVALID),
        (float("inf"), DistanceBucket.INVALID),
    ],
)
def test_bucket_boundaries(d, expected) -> None:
    assert select_distance_bucket(d) == expected


def test_distance_pct_formula() -> None:
    # first=100, full=98 → 2%
    assert compute_original_distance_pct(100.0, 98.0) == pytest.approx(2.0)
    assert compute_original_distance_pct(0.0, 1.0) is None
    assert compute_original_distance_pct(1.0, float("nan")) is None
    assert compute_original_distance_pct(-1.0, 1.0) is None


def test_non_positive_distance_no_policy() -> None:
    assert select_adaptive_policy("adaptive_equal", 0.0) is None
    assert select_adaptive_policy("adaptive_equal", None) is None
    assert select_adaptive_policy("adaptive_backloaded", float("nan")) is None


def test_distance_status_taxonomy() -> None:
    assert (
        classify_distance_status(
            profile="adaptive_equal",
            max_cycle=3,
            distance_pct=None,
            bucket=None,
            has_c4_followup_plan=False,
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
        )
        == "fixed_profile_no_adaptive_bucket"
    )
    assert (
        classify_distance_status(
            profile="adaptive_equal",
            max_cycle=4,
            distance_pct=5.5,
            bucket=DistanceBucket.D_4_7,
            has_c4_followup_plan=True,
            plan_accepted=True,
        )
        == "4_7"
    )
    assert (
        classify_distance_status(
            profile="legacy",
            max_cycle=0,
            distance_pct=None,
            bucket=None,
            has_c4_followup_plan=False,
        )
        == "none"
    )


def test_adaptive_equal_policies() -> None:
    p = select_adaptive_policy("adaptive_equal", 1.0)
    assert p is not None
    assert p.price_fractions == (0.50, 1.00)
    assert p.qty_fractions == (0.50, 0.50)
    p = select_adaptive_policy("adaptive_equal", 3.0)
    assert p is not None
    assert p.price_fractions == (0.33, 0.66, 1.00)
    p = select_adaptive_policy("adaptive_equal", 5.0)
    assert p is not None
    assert p.stage_count == 4
    p = select_adaptive_policy("adaptive_equal", 8.0)
    assert p is not None
    assert p.price_fractions[-1] == 1.0


def test_adaptive_backloaded_policies() -> None:
    p = select_adaptive_policy("adaptive_backloaded", 1.5)
    assert p is not None
    assert p.qty_fractions == (0.35, 0.65)
    p = select_adaptive_policy("adaptive_backloaded", 3.0)
    assert p is not None
    assert p.price_fractions == (0.25, 0.55, 1.00)
    p = select_adaptive_policy("adaptive_backloaded", 6.0)
    assert p is not None
    assert p.qty_fractions == (0.15, 0.20, 0.25, 0.40)


def test_price_fractions_strictly_increasing_and_end_1() -> None:
    for name in ("adaptive_equal", "adaptive_backloaded"):
        for d in (1.0, 3.0, 5.0, 9.0):
            p = select_adaptive_policy(name, d)
            assert p is not None
            fr = p.price_fractions
            assert all(fr[i] < fr[i + 1] for i in range(len(fr) - 1))
            assert abs(fr[-1] - 1.0) < 1e-12
            assert all(0 < f <= 1 for f in fr)


def test_long_primary_price_direction() -> None:
    p0, p_full = 2.0, 1.8
    mid = price_at_fraction(
        first_leg_fill=p0,
        full_trigger=p_full,
        fraction=0.5,
        direction="long_primary_short_reduce",
    )
    assert p_full < mid < p0


def test_residual_coverage_qty_sum() -> None:
    base = adaptive_base_config("adaptive_equal")
    policy = select_adaptive_policy("adaptive_equal", 5.0)
    assert policy is not None
    cfg = config_with_adaptive_policy(base, policy)
    plan = build_stage_plan(
        config=cfg,
        cycle_index=4,
        purpose="CYCLE_4_SHORT_REDUCE",
        first_leg_fill_price=2.0,
        full_trigger_price=1.88,  # 6%
        total_qty=40.0,
        required_net=2.0,
        short_entry_price=2.05,
        fee_rate=0.00055,
        price_tick=0.0001,
        qty_step=0.01,
        min_order_qty=0.01,
    )
    assert plan.accepted
    assert abs(sum(s.qty for s in plan.stages) - 40.0) <= 0.01 + 1e-9
    assert plan.stages[-1].qty > 0


def test_min_notional_reduce_stage_count() -> None:
    base = adaptive_base_config("adaptive_equal")
    policy = select_adaptive_policy("adaptive_equal", 8.0)
    assert policy is not None
    cfg = config_with_adaptive_policy(base, policy)
    plan = build_stage_plan(
        config=cfg,
        cycle_index=4,
        purpose="CYCLE_4_SHORT_REDUCE",
        first_leg_fill_price=2.0,
        full_trigger_price=1.8,
        total_qty=3.0,  # tiny notionals
        required_net=0.2,
        short_entry_price=2.05,
        fee_rate=0.00055,
        price_tick=0.0001,
        qty_step=0.01,
        min_order_qty=0.01,
    )
    assert plan.accepted
    assert plan.stage_count < policy.stage_count or plan.fallback_used == "reduce_stage_count" or plan.stage_count <= 1


def test_legacy_and_tem_unchanged() -> None:
    leg = resolve_profile("legacy")
    assert leg.enabled is False
    assert leg.adaptive is False
    tem = resolve_grid_profile("two_early_medium")
    assert tem.enabled is True
    assert tem.adaptive is False
    assert tem.price_distribution.fractions == (0.40, 1.00)
    assert tem.qty_distribution.fractions == (0.35, 0.65)
    assert tem.only_cycles == (4,)
    assert tem.apply_to == ("long_primary_short_reduce",)
    assert tem.last_stage_mode == "residual_coverage"
    assert validate_config(tem) == []


def test_adaptive_profiles_only_cycle4_long_primary() -> None:
    for name in ("adaptive_equal", "adaptive_backloaded"):
        assert is_adaptive_profile(name)
        cfg = resolve_grid_profile(name)
        assert cfg.enabled is True
        assert cfg.adaptive is True
        assert cfg.only_cycles == (4,)
        assert cfg.apply_to == ("long_primary_short_reduce",)
        assert cfg.last_stage_mode == "residual_coverage"
        assert cfg.insufficient_size_fallback == "reduce_stage_count"


def test_disabled_shim_no_wrap_legacy() -> None:
    config_load = resolve_backtest_config(config_source="test", signal="long", symbol="APTUSDT")
    strategy = build_strategy("long", config_load.config)
    install_second_leg_price_staging(strategy, resolve_profile("legacy"))
    assert getattr(strategy, "_backtest_slps_shim_installed", False) is False


def test_pair_key_parity_helpers() -> None:
    assert window_pair_key("aptusdt", "early", 100) == "APTUSDT|early|100"
    keys = [
        window_profile_run_key("APTUSDT", "early", 100, p)
        for p in ("legacy", "two_early_medium", "adaptive_equal", "adaptive_backloaded")
    ]
    assert len(keys) == len(set(keys))
    assert all(k.startswith("APTUSDT|early|100|") for k in keys)


def test_tick_qty_rounding_monotone_prices() -> None:
    base = adaptive_base_config("adaptive_backloaded")
    policy = select_adaptive_policy("adaptive_backloaded", 6.0)
    assert policy is not None
    cfg = config_with_adaptive_policy(base, policy)
    plan = build_stage_plan(
        config=cfg,
        cycle_index=4,
        purpose="CYCLE_4_SHORT_REDUCE",
        first_leg_fill_price=1.2345,
        full_trigger_price=1.1500,
        total_qty=25.0,
        required_net=1.0,
        short_entry_price=1.25,
        fee_rate=0.00055,
        price_tick=0.0001,
        qty_step=0.01,
        min_order_qty=0.01,
    )
    assert plan.accepted
    prices = [s.trigger_price for s in plan.stages]
    assert prices == sorted(prices, reverse=True)
    assert len(set(round(p, 6) for p in prices)) == len(prices)


def test_checkpoint_resume_four_profiles_concept(tmp_path: Path) -> None:
    from research.backtests.multicoin_price_staging_grid import (
        assert_output_dir_safe,
        atomic_write_json,
        load_checkpoint,
    )
    from research.backtests.run_adaptive_distance_staging_validation import (
        PROFILES,
        _empty_checkpoint,
    )

    out = tmp_path / "ads"
    out.mkdir()
    assert_output_dir_safe(out, resume=True)
    ck = _empty_checkpoint(coins=["APTUSDT"], planned_pairs=2)
    ck["profiles"] = list(PROFILES)
    ck["completed_run_keys"] = ["APTUSDT|early|10|legacy"]
    atomic_write_json(out / "checkpoint.json", ck)
    loaded = load_checkpoint(out / "checkpoint.json")
    assert loaded is not None
    assert loaded["profiles"] == list(PROFILES)
    assert "APTUSDT|early|10|legacy" in loaded["completed_run_keys"]

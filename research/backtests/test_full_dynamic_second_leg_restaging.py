"""Unit tests for FULL_DYNAMIC residual SHORT_REDUCE restaging (research-only)."""

from __future__ import annotations

import pytest

from research.backtests.full_dynamic_second_leg_restaging import (
    ECONOMIC_TOLERANCE_USDT,
    build_residual_stage_plan,
    compute_canonical_economics,
    recompute_required_qty,
    resolve_full_dynamic_profile,
    select_replan_config,
    trigger_for_target_net,
)
from research.backtests.second_leg_price_staging import (
    resolve_grid_profile,
    short_reduce_expected_net,
)


def test_canonical_remaining_drops_exactly_after_fill() -> None:
    eco0 = compute_canonical_economics(
        required_net_total=11.4017,
        confirmed_stage_realized_net=0.0,
        initial_pending_cycle_loss_usdt=11.3867,
        target_profit_usdt=0.015,
    )
    assert eco0.remaining_required_net == pytest.approx(11.4017)
    fill_net = 1.8205817615669844
    eco1 = compute_canonical_economics(
        required_net_total=11.4017,
        confirmed_stage_realized_net=fill_net,
        initial_pending_cycle_loss_usdt=11.3867,
        target_profit_usdt=0.015,
    )
    assert eco1.remaining_required_net == pytest.approx(11.4017 - fill_net)
    assert eco1.pending_cycle_loss_usdt == pytest.approx(max(11.3867 - fill_net, 0.0))
    assert eco1.pending_cycle_loss_usdt == pytest.approx(
        max(eco1.remaining_required_net - 0.015, 0.0), abs=1e-9
    )


def test_confirmed_stage_net_not_double_counted_in_formula() -> None:
    eco = compute_canonical_economics(
        required_net_total=10.0,
        confirmed_stage_realized_net=4.0,
        initial_pending_cycle_loss_usdt=9.985,
        target_profit_usdt=0.015,
    )
    eco2 = compute_canonical_economics(
        required_net_total=10.0,
        confirmed_stage_realized_net=4.0,
        initial_pending_cycle_loss_usdt=9.985,
        target_profit_usdt=0.015,
    )
    assert eco.remaining_required_net == eco2.remaining_required_net == pytest.approx(6.0)


def test_full_coverage_yields_zero_remaining_qty() -> None:
    eco = compute_canonical_economics(
        required_net_total=5.0,
        confirmed_stage_realized_net=5.0,
        initial_pending_cycle_loss_usdt=4.985,
        target_profit_usdt=0.015,
    )
    assert eco.is_covered
    assert eco.remaining_required_net <= ECONOMIC_TOLERANCE_USDT
    qty, _ = recompute_required_qty(
        remaining_required_net=eco.remaining_required_net,
        short_entry=1.8,
        full_trigger=1.5,
        fee_rate=0.00055,
        actual_short_qty=100.0,
        prior_remaining_stage_qty=50.0,
        qty_step=0.001,
    )
    assert qty == 0.0


def test_recomputed_qty_never_exceeds_prior_or_actual() -> None:
    qty, _ = recompute_required_qty(
        remaining_required_net=1000.0,
        short_entry=1.8,
        full_trigger=1.5,
        fee_rate=0.00055,
        actual_short_qty=20.0,
        prior_remaining_stage_qty=10.0,
        qty_step=0.001,
    )
    assert qty <= 10.0 + 1e-12
    assert qty <= 20.0 + 1e-12


def test_trigger_invert_roundtrip() -> None:
    entry, qty, fee, target = 1.7912, 50.0, 0.00055, 8.5
    trig = trigger_for_target_net(
        target_net=target, short_entry=entry, qty=qty, fee_rate=fee
    )
    net = short_reduce_expected_net(
        short_entry=entry, trigger=trig, qty=qty, fee_rate=fee
    )
    assert net == pytest.approx(target, rel=1e-9, abs=1e-9)


def test_tem_replan_changes_prices_after_anchor() -> None:
    cfg = resolve_full_dynamic_profile("two_early_medium_full_dynamic")
    plan, full, reason = build_residual_stage_plan(
        config=cfg,
        cycle_index=4,
        purpose="CYCLE_4_SHORT_REDUCE",
        anchor_price=1.6361,
        remaining_required_net=5.9,
        remaining_qty=66.75,
        short_entry=1.7912,
        fee_rate=0.00055,
        price_tick=0.0001,
        qty_step=0.001,
        min_order_qty=0.001,
        prior_full_trigger=1.5673,
    )
    assert plan is not None and plan.accepted
    assert plan.stage_count >= 1
    assert all(s.trigger_price < 1.6361 - 1e-12 for s in plan.stages)


def test_adaptive_and_fixed_step_replan_produce_stages() -> None:
    for name in (
        "adaptive_equal_full_dynamic",
        "fixed_step_1pct_equal_full_dynamic",
    ):
        cfg = resolve_full_dynamic_profile(name)
        plan, full, reason = build_residual_stage_plan(
            config=cfg,
            cycle_index=4,
            purpose="CYCLE_4_SHORT_REDUCE",
            anchor_price=1.6652,
            remaining_required_net=9.5,
            remaining_qty=88.0,
            short_entry=1.7912,
            fee_rate=0.00055,
            price_tick=0.0001,
            qty_step=0.001,
            min_order_qty=0.001,
            prior_full_trigger=1.5673,
        )
        assert plan is not None and plan.accepted, (name, reason)
        assert plan.total_qty <= 88.0 + 1e-9
        assert all(s.trigger_price < 1.6652 for s in plan.stages)


def test_partial_dynamic_profiles_untouched() -> None:
    for name in ("two_early_medium", "adaptive_equal", "fixed_step_1pct_equal"):
        cfg = resolve_grid_profile(name)
        assert cfg.full_dynamic is False
        assert cfg.enabled is True


def test_full_dynamic_profiles_flag() -> None:
    for name in (
        "two_early_medium_full_dynamic",
        "adaptive_equal_full_dynamic",
        "fixed_step_1pct_equal_full_dynamic",
    ):
        cfg = resolve_grid_profile(name)
        assert cfg.full_dynamic is True
        assert cfg.profile_name == name


def test_select_replan_config_tem_collapses_tiny_distance() -> None:
    cfg = resolve_full_dynamic_profile("two_early_medium_full_dynamic")
    out = select_replan_config(
        profile_name=cfg.profile_name,
        anchor_price=1.57,
        full_trigger=1.5699,
        base_cfg=cfg,
    )
    assert out.stage_count == 1


def test_gold_apt_4026_full_dynamic_gates() -> None:
    """Integration: TEM full_dynamic restages on the gold start."""
    import csv
    from pathlib import Path

    from research.backtests.candle_loader import load_candles_for_symbol
    from research.backtests.historical_backtest import normalize_candles
    from research.backtests.multicoin_blocker_price_staging import (
        FULL_HISTORY_CANDLE_LIMIT,
        run_isolated_blocker,
    )

    source = Path(
        "research/backtests/results/fixed_step_distance_staging_large_1000_500_20260722/start_points.csv"
    )
    if not source.exists():
        pytest.skip("full-run start_points missing")
    starts = {r["pair_key"]: r for r in csv.DictReader(source.open())}
    sp = starts["APTUSDT|early|4026"]
    si = int(sp["start_index"])
    mw = int(float(sp["max_window_candles"]))
    candles = normalize_candles(
        "APTUSDT", load_candles_for_symbol("APTUSDT", limit=FULL_HISTORY_CANDLE_LIMIT)
    )
    series = candles[: si + mw]
    cfg = resolve_full_dynamic_profile("two_early_medium_full_dynamic")
    result = run_isolated_blocker(
        coin="APTUSDT", candles=series, start_index=si, staging_config=cfg
    )
    assert result.final_status != "error"
    ex = result.final_strategy_state_excerpt or {}
    events = list(ex.get("research_fd_replan_events") or [])
    assert events, "expected at least one replan event"
    assert any(int(e.get("plan_revision") or 0) >= 1 for e in events)
    assert any(
        float(e.get("remaining_required_after") or 0)
        < float(e.get("remaining_required_before") or 0) - 1e-9
        for e in events
    )
    e0 = events[0]
    assert list(e0.get("new_stage_prices") or [])
    assert int(e0.get("new_stage_eligible_from_candle") or 0) >= int(
        e0.get("candle_index") or 0
    ) + 1


def test_partial_dynamic_still_static_residuals_on_gold() -> None:
    import csv
    from pathlib import Path

    from research.backtests.candle_loader import load_candles_for_symbol
    from research.backtests.historical_backtest import normalize_candles
    from research.backtests.multicoin_blocker_price_staging import (
        FULL_HISTORY_CANDLE_LIMIT,
        run_isolated_blocker,
    )

    source = Path(
        "research/backtests/results/fixed_step_distance_staging_large_1000_500_20260722/start_points.csv"
    )
    if not source.exists():
        pytest.skip("full-run start_points missing")
    starts = {r["pair_key"]: r for r in csv.DictReader(source.open())}
    sp = starts["APTUSDT|early|4026"]
    si = int(sp["start_index"])
    mw = int(float(sp["max_window_candles"]))
    candles = normalize_candles(
        "APTUSDT", load_candles_for_symbol("APTUSDT", limit=FULL_HISTORY_CANDLE_LIMIT)
    )
    series = candles[: si + mw]
    cfg = resolve_grid_profile("two_early_medium")
    result = run_isolated_blocker(
        coin="APTUSDT", candles=series, start_index=si, staging_config=cfg
    )
    ex = result.final_strategy_state_excerpt or {}
    assert not (ex.get("research_fd_replan_events") or [])
    # Partial: exactly one creation batch of 2 stages at candle 30
    creates = [
        i
        for i in (result.intent_log or [])
        if str(i.get("purpose")) == "CYCLE_4_SHORT_REDUCE"
        and (i.get("metadata_excerpt") or {}).get("is_staged_second_leg_tp")
    ]
    assert len(creates) == 2

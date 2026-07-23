"""Unit tests for research second-leg price staging planner + guards."""

from __future__ import annotations

from pathlib import Path

import pytest

from fixed_cycle_hedge_bot.models import StrategyIntent
from research.backtests.second_leg_price_staging import (
    build_stage_plan,
    dedupe_staged_intents_by_identity,
    price_at_fraction,
    profile_linear4,
    resolve_profile,
    stage_identity_key,
    validate_config,
)
from research.backtests.second_leg_price_staging_shim import install_second_leg_price_staging
from research.backtests.apt_t3_short_reduce_price_staging_lab import (
    APT_TRADE3_START_INDEX,
    assert_output_dir_safe,
    parse_profiles,
    parse_sizes,
    run_lab_backtest,
)
from research.backtests.apt_baseline_blocker_root_cause import check_baseline_parity
from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.historical_backtest import normalize_candles
from research.backtests.long_add_multistart_metrics import analyze_trade
from research.backtests.backtest_config_loader import resolve_backtest_config
from research.backtests.hedge_bot_original_simulator import build_strategy


def test_legacy_profile_disabled() -> None:
    cfg = resolve_profile("legacy")
    assert cfg.enabled is False
    assert validate_config(cfg) == []


def test_price_fraction_long_primary_example() -> None:
    # P_first=1.80, P_full=1.6669, f=0.25 → ~1.7667
    p = price_at_fraction(
        first_leg_fill=1.80,
        full_trigger=1.6669,
        fraction=0.25,
        direction="long_primary_short_reduce",
    )
    assert abs(p - 1.766725) < 1e-6


def test_linear4_plan_distinct_prices() -> None:
    cfg = profile_linear4()
    plan = build_stage_plan(
        config=cfg,
        cycle_index=4,
        purpose="CYCLE_4_SHORT_REDUCE",
        first_leg_fill_price=1.80,
        full_trigger_price=1.6669,
        total_qty=20.0,
        required_net=1.5,
        short_entry_price=1.90,
        fee_rate=0.00055,
        price_tick=0.0001,
        qty_step=0.01,
        min_order_qty=0.01,
    )
    assert plan.accepted
    assert plan.stage_count >= 2
    prices = [s.trigger_price for s in plan.stages]
    assert prices == sorted(prices, reverse=True)
    assert abs(sum(s.qty for s in plan.stages) - 20.0) <= 0.01 + 1e-9
    assert len({round(p, 6) for p in prices}) == len(prices)


def test_min_notional_reduces_stage_count() -> None:
    cfg = profile_linear4()
    plan = build_stage_plan(
        config=cfg,
        cycle_index=4,
        purpose="CYCLE_4_SHORT_REDUCE",
        first_leg_fill_price=1.80,
        full_trigger_price=1.6669,
        total_qty=4.0,  # ~6.7 USDT total → cannot fit 4×5
        required_net=0.5,
        short_entry_price=1.90,
        fee_rate=0.00055,
        price_tick=0.0001,
        qty_step=0.01,
        min_order_qty=0.01,
    )
    assert plan.accepted
    assert plan.stage_count <= 1 or plan.fallback_used == "reduce_stage_count"


def test_invalid_fractions_rejected() -> None:
    cfg = profile_linear4()
    from dataclasses import replace
    from research.backtests.second_leg_price_staging import PriceDistribution

    bad = replace(
        cfg,
        price_distribution=PriceDistribution(mode="custom_fractions", fractions=(0.5, 0.25, 0.75, 1.0)),
    )
    errs = validate_config(bad)
    assert any("increasing" in e for e in errs)


def test_dedupe_keeps_distinct_stage_index() -> None:
    intents = [
        StrategyIntent(
            side="short",
            qty=1.0,
            purpose="CYCLE_4_SHORT_REDUCE",
            trigger_price=1.77,
            metadata={"is_staged_second_leg_tp": True, "stage_index": 0, "research_price_staging": True},
        ),
        StrategyIntent(
            side="short",
            qty=1.0,
            purpose="CYCLE_4_SHORT_REDUCE",
            trigger_price=1.73,
            metadata={"is_staged_second_leg_tp": True, "stage_index": 1, "research_price_staging": True},
        ),
        StrategyIntent(
            side="short",
            qty=1.0,
            purpose="CYCLE_4_SHORT_REDUCE",
            trigger_price=1.77,
            metadata={"is_staged_second_leg_tp": True, "stage_index": 0, "research_price_staging": True},
        ),
    ]
    out = dedupe_staged_intents_by_identity(intents, cycle_index=4, purpose="CYCLE_4_SHORT_REDUCE")
    assert len(out) == 2
    assert stage_identity_key(cycle_index=4, purpose="CYCLE_4_SHORT_REDUCE", stage_index=0) == (
        4,
        "CYCLE_4_SHORT_REDUCE",
        0,
    )


def test_disabled_shim_does_not_wrap() -> None:
    config_load = resolve_backtest_config(config_source="test", signal="long", symbol="APTUSDT")
    strategy = build_strategy("long", config_load.config)
    install_second_leg_price_staging(strategy, resolve_profile("legacy"))
    assert getattr(strategy, "_backtest_slps_shim_installed", False) is False
    assert getattr(strategy, "_backtest_slps_config").enabled is False


def test_parse_helpers() -> None:
    assert parse_sizes("100:50,500:250")[1][0] == "S500"
    assert [c.profile_name for c in parse_profiles("legacy,linear4")] == ["legacy", "linear4"]


def test_assert_refuses_protected() -> None:
    from research.backtests.apt_t3_short_reduce_price_staging_lab import PROTECTED

    with pytest.raises(RuntimeError, match="protected"):
        assert_output_dir_safe(PROTECTED[0])


@pytest.fixture(scope="module")
def apt_candles():
    return normalize_candles("APTUSDT", load_candles_for_symbol("APTUSDT", limit=50000))


def test_legacy_parity_s100(apt_candles) -> None:
    cfg = resolve_profile("legacy")
    result = run_lab_backtest(
        candles=apt_candles,
        start_index=APT_TRADE3_START_INDEX,
        base_notional_usdt=100.0,
        staging_config=cfg,
    )
    analysis = analyze_trade(
        result,
        variant="legacy_S100",
        long_add_pct=0.5,
        target_profit_usdt=0.015,
        window_candles=apt_candles[APT_TRADE3_START_INDEX:],
        valid=True,
        skip_reason="ok",
    )
    parity = check_baseline_parity(coin="APTUSDT", trade_id=3, result=result, analysis=analysis)
    assert parity["ok"] is True


def test_linear4_emits_distinct_triggers_when_size_allows(apt_candles) -> None:
    # Use 1000/500 so min-notional can accept multiple stages; only_cycles=(4,).
    cfg = resolve_profile("linear4")
    result = run_lab_backtest(
        candles=apt_candles,
        start_index=APT_TRADE3_START_INDEX,
        base_notional_usdt=1000.0,
        staging_config=cfg,
    )
    triggers = []
    for intent in result.intent_log or []:
        if str(intent.get("purpose") or "") != "CYCLE_4_SHORT_REDUCE":
            continue
        meta = dict(intent.get("metadata_excerpt") or {})
        if meta.get("research_price_staging") or meta.get("is_staged_second_leg_tp"):
            triggers.append(float(intent.get("trigger_price") or 0.0))
    c4_long = any(
        str(f.get("purpose") or "") == "CYCLE_4_LONG_ADD" for f in (result.fill_log or [])
    )
    assert c4_long
    assert len(set(round(t, 6) for t in triggers)) >= 2
    # Early stages should fill before the bounce path closes the trade.
    staged_fills = [
        f
        for f in (result.fill_log or [])
        if str(f.get("purpose") or "") == "CYCLE_4_SHORT_REDUCE"
        and (f.get("metadata_excerpt") or {}).get("stage_index") is not None
    ]
    assert len(staged_fills) >= 1

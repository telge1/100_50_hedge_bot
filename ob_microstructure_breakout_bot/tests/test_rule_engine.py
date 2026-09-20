from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ob_microstructure_breakout_bot.backtest.cases_doge import list_cases
from ob_microstructure_breakout_bot.backtest.runner import _pass, _run_case
from ob_microstructure_breakout_bot.ema import (
    classify_ema_exit,
    is_ema200_noise_dip,
    trend_still_intact,
)
from ob_microstructure_breakout_bot.models import (
    BreakoutTier,
    EmaSnapshot,
    MarketState,
    ObBandSnapshot,
    TradeWindowStats,
)
from ob_microstructure_breakout_bot.rule_engine import RuleEngine, classify_long_breakout
from ob_microstructure_breakout_bot.thresholds import load_thresholds


def test_load_doge_thresholds():
    t = load_thresholds("DOGEUSDT")
    assert t.symbol == "DOGEUSDT"
    assert t.tier2_min_confirm_delta > t.tier1_min_confirm_delta


def test_bullish_stack_hold():
    ema = EmaSnapshot(ema9=0.086, ema20=0.0855, ema59=0.085, price=0.0849)
    assert trend_still_intact(ema)
    res = classify_ema_exit(ema)
    assert res is not None
    assert res.state == MarketState.HOLD


def test_ema9_exit_warning():
    ema = EmaSnapshot(ema9=0.0848, ema20=0.0852, ema59=0.0850, price=0.0847)
    res = classify_ema_exit(ema, sell_followthrough=False)
    assert res is not None
    assert res.state == MarketState.EXIT_WARNING


def test_ema9_with_sell_followthrough_confirms_exit():
    ema = EmaSnapshot(ema9=0.0848, ema20=0.0852, ema59=0.0850, price=0.0847)
    res = classify_ema_exit(ema, sell_followthrough=True)
    assert res is not None
    assert res.state == MarketState.EXIT_CONFIRMED


def test_ema200_price_sweep_holds_while_emas_above():
    # Chart box 2: price dumps under EMA200, EMA9/20 still far above, EMA20 > EMA59
    ema = EmaSnapshot(
        ema9=0.090,
        ema20=0.089,
        ema59=0.088,
        price=0.0865,
        ema200=0.087,
    )
    assert trend_still_intact(ema)
    res = classify_ema_exit(ema)
    assert res is not None
    assert res.state == MarketState.HOLD
    assert "EMA200" in " ".join(res.reasons)


def test_brief_ema9_cross_under_ema200_ignored():
    previous = EmaSnapshot(
        ema9=0.0875,
        ema20=0.0880,
        ema59=0.0870,
        price=0.0876,
        ema200=0.0872,
    )
    current = EmaSnapshot(
        ema9=0.0870,  # one-bar dip under EMA200
        ema20=0.0879,
        ema59=0.0875,  # still above EMA200
        price=0.0868,
        ema200=0.0872,
    )
    assert is_ema200_noise_dip(current, previous=previous)
    assert trend_still_intact(current)
    res = classify_ema_exit(current, previous=previous)
    assert res is not None
    assert res.state == MarketState.HOLD


def test_ema20_below_ema59_exits_even_with_ema200():
    ema = EmaSnapshot(
        ema9=0.086,
        ema20=0.085,
        ema59=0.0865,
        price=0.0855,
        ema200=0.084,
    )
    res = classify_ema_exit(ema, sell_followthrough=True)
    assert res is not None
    assert res.state == MarketState.EXIT_CONFIRMED


def test_ema59_below_ema200_exits():
    ema = EmaSnapshot(
        ema9=0.086,
        ema20=0.0855,
        ema59=0.0845,
        price=0.085,
        ema200=0.0850,
    )
    res = classify_ema_exit(
        ema,
        sell_followthrough=True,
        ob=ObBandSnapshot(40_000, 90_000, 0, 0),
    )
    assert res is not None
    assert res.state == MarketState.EXIT_CONFIRMED
    assert "EMA200" in " ".join(res.reasons)


def test_macro_bias_holds_when_ema9_soft_but_ema20_above_ema200():
    ema = EmaSnapshot(
        ema9=0.0868,  # soft vs EMA59
        ema20=0.0875,
        ema59=0.0870,
        price=0.0869,
        ema200=0.0860,
    )
    assert trend_still_intact(ema)
    res = classify_ema_exit(ema, sell_followthrough=False)
    assert res is not None
    assert res.state == MarketState.HOLD


def test_tier2_strong_breakout():
    t = load_thresholds("DOGEUSDT")
    res = classify_long_breakout(
        thresholds=t,
        confirm=TradeWindowStats(389_400, 155_400),
        ob_at_event=ObBandSnapshot(114_000, 40_600, 0, 0),
    )
    assert res.state == MarketState.BREAKOUT_CONFIRMED
    assert res.tier == BreakoutTier.STRONG


def test_fakeout_followthrough_flip():
    t = load_thresholds("DOGEUSDT")
    res = classify_long_breakout(
        thresholds=t,
        confirm=TradeWindowStats(200_000, 50_000),
        followthrough=TradeWindowStats(50_000, 200_000),
        ob_at_event=ObBandSnapshot(90_000, 50_000, 0, 0),
    )
    assert res.state == MarketState.FAKEOUT
    assert res.tier == BreakoutTier.FAKEOUT


def test_all_known_doge_cases_pass():
    engine = RuleEngine(load_thresholds("DOGEUSDT"))
    for case in list_cases():
        result = _run_case(engine, case)
        assert _pass(case, result), (
            f"{case.case_id}: expected {case.expected_state}/{case.expected_tier}, "
            f"got {result.state}/{result.tier} reasons={result.reasons}"
        )

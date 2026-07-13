"""Unit tests for research-only 15m Direction Gate."""

from __future__ import annotations

import pandas as pd
import pytest

from research.regime_scanner.direction_gate import (
    BarFeatures,
    DirectionGateConfig,
    GateRuntimeState,
    assert_outcomes_do_not_affect_gate,
    evaluate_entry_scores,
    expand_15m_state_to_5m_decisions,
    step_gate,
    would_block,
)
from research.regime_scanner.timeframes import aggregate_candles


def _feat(**kwargs) -> BarFeatures:
    base = dict(
        bar_open="2026-03-06T15:00:00+00:00",
        bar_close_time="2026-03-06T15:15:00+00:00",
        close=1.0,
        ema_9=1.0,
        ema_20=1.0,
        ema_59=1.0,
        ema_200=1.0,
        ema20_slope=0.0,
        ema59_slope=0.0,
        adx=20.0,
        plus_di=10.0,
        minus_di=20.0,
        atr_pct=0.5,
        last_swing_high=1.05,
        last_swing_low=0.98,
        last_swing_high_ts=None,
        last_swing_low_ts=None,
        structure_high=None,
        structure_low=None,
        warmup_ok=True,
    )
    base.update(kwargs)
    return BarFeatures(**base)  # type: ignore[arg-type]


def _bearish_trend_bits(f: BarFeatures) -> BarFeatures:
    f.close = 0.95
    f.ema_9 = 0.96
    f.ema_20 = 0.97
    f.ema_59 = 0.99
    f.ema20_slope = -0.2
    f.ema59_slope = -0.1
    f.close_lt_ema20 = True
    f.close_gt_ema20 = False
    f.ema9_lt_ema20 = True
    f.ema9_gt_ema20 = False
    f.ema20_lt_ema59 = True
    f.ema20_gt_ema59 = False
    f.ema9_lt_ema20_lt_ema59 = True
    f.ema9_gt_ema20_gt_ema59 = False
    f.ema20_slope_neg = True
    f.ema20_slope_pos = False
    f.ema59_slope_neg = True
    f.ema59_slope_pos = False
    f.di_bearish = True
    f.di_bullish = False
    f.adx_ok = True
    f.atr_ok = True
    return f


def _bullish_trend_bits(f: BarFeatures) -> BarFeatures:
    f.close = 1.05
    f.ema_9 = 1.04
    f.ema_20 = 1.03
    f.ema_59 = 1.01
    f.ema20_slope = 0.2
    f.ema59_slope = 0.1
    f.close_lt_ema20 = False
    f.close_gt_ema20 = True
    f.ema9_lt_ema20 = False
    f.ema9_gt_ema20 = True
    f.ema20_lt_ema59 = False
    f.ema20_gt_ema59 = True
    f.ema9_lt_ema20_lt_ema59 = False
    f.ema9_gt_ema20_gt_ema59 = True
    f.ema20_slope_neg = False
    f.ema20_slope_pos = True
    f.ema59_slope_neg = False
    f.ema59_slope_pos = True
    f.di_bearish = False
    f.di_bullish = True
    f.adx_ok = True
    f.atr_ok = True
    return f


def test_default_gate_disabled() -> None:
    assert DirectionGateConfig().enabled is False


def test_forming_15m_not_visible() -> None:
    rows = []
    t0 = pd.Timestamp("2026-03-06T14:00:00+00:00")
    for i in range(10):
        ts = t0 + pd.Timedelta(minutes=5 * i)
        rows.append(
            {
                "timestamp": ts,
                "open": 1.0,
                "high": 1.01,
                "low": 0.99,
                "close": 1.0,
                "volume": 1.0,
            }
        )
    c = pd.DataFrame(rows)
    decision = pd.Timestamp("2026-03-06T14:10:00+00:00")
    agg = aggregate_candles(c, "15m", decision)
    opens = set(pd.to_datetime(agg["timestamp"], utc=True)) if len(agg) else set()
    assert pd.Timestamp("2026-03-06T14:00:00+00:00") not in opens


def test_state_updates_only_on_closed_15m_via_expand() -> None:
    g15 = pd.DataFrame(
        {
            "bar_close_time": pd.to_datetime(
                ["2026-03-06T15:15:00+00:00", "2026-03-06T15:30:00+00:00"], utc=True
            ),
            "direction_gate_state": ["neutral", "strong_bearish"],
            "would_block_long": [False, True],
            "would_block_short": [False, False],
            "gate_variant": ["B1", "B1"],
            "close": [1.0, 0.95],
        }
    )
    decisions = pd.to_datetime(
        [
            "2026-03-06T15:20:00+00:00",
            "2026-03-06T15:25:00+00:00",
            "2026-03-06T15:30:00+00:00",
            "2026-03-06T15:35:00+00:00",
        ],
        utc=True,
    )
    m = expand_15m_state_to_5m_decisions(g15, decisions)
    assert list(m["direction_gate_state"]) == [
        "neutral",
        "neutral",
        "strong_bearish",
        "strong_bearish",
    ]


def test_b1_ema_without_enough_confirms_insufficient() -> None:
    f = _bearish_trend_bits(_feat())
    f.lower_high = f.lower_low = f.break_below_swing_low = False
    f.break_prior_day_low = False
    f.ema20_lt_ema59 = False
    f.ema59_slope_neg = False
    f.di_bearish = False
    f.adx_ok = False
    cfg = DirectionGateConfig(variant="B1", use_prior_day_low_break=False)
    s = evaluate_entry_scores(f, cfg)
    assert s["bearish_required_ok"] is True
    assert s["bearish_entry"] is False


def test_b2_requires_structure_and_break() -> None:
    f = _bearish_trend_bits(_feat())
    f.lower_high = False
    f.break_below_swing_low = True
    cfg = DirectionGateConfig(variant="B2")
    assert evaluate_entry_scores(f, cfg)["bearish_entry"] is False
    f.lower_high = True
    f.break_below_swing_low = False
    assert evaluate_entry_scores(f, cfg)["bearish_entry"] is False
    f.break_below_swing_low = True
    assert evaluate_entry_scores(f, cfg)["bearish_entry"] is True


def test_b3_requires_structure_confirm() -> None:
    f = _bearish_trend_bits(_feat())
    f.lower_high = f.lower_low = f.break_below_swing_low = False
    cfg = DirectionGateConfig(variant="B3")
    assert evaluate_entry_scores(f, cfg)["bearish_required_ok"] is True
    assert evaluate_entry_scores(f, cfg)["bearish_entry"] is False
    f.lower_high = True
    assert evaluate_entry_scores(f, cfg)["bearish_entry"] is True


def test_lh_ll_break_flags() -> None:
    f = _feat(lower_high=True, lower_low=True, break_below_swing_low=True)
    assert f.lower_high and f.lower_low and f.break_below_swing_low


def test_single_green_candle_does_not_exit_bearish() -> None:
    cfg = DirectionGateConfig(variant="B1", min_hold_bars=2)
    rt = GateRuntimeState(state="strong_bearish", age_bars=3, entered_at="t0")
    f = _bearish_trend_bits(_feat())
    f.close_gt_ema20 = True
    f.close_lt_ema20 = False
    score = evaluate_entry_scores(f, cfg)
    rt, meta = step_gate(rt, f, score, cfg)
    assert rt.state == "strong_bearish"
    assert meta["transition"] is None


def test_two_closes_above_ema20_exit_bearish() -> None:
    cfg = DirectionGateConfig(variant="B1", min_hold_bars=2)
    rt = GateRuntimeState(state="strong_bearish", age_bars=2, entered_at="t0")
    f = _feat(warmup_ok=True, close_gt_ema20=True, close_lt_ema20=False, ema_9=1.0, ema_20=1.0)
    score = {
        "bearish_entry": False,
        "bullish_entry": False,
        "bullish_required_ok": False,
        "bearish_required_ok": False,
        "bullish_confirm_count": 0,
        "bearish_confirm_count": 0,
        "bullish_need": 2,
        "bearish_need": 2,
    }
    rt, _ = step_gate(rt, f, score, cfg)
    assert rt.consec_close_above_ema20 == 1
    assert rt.state == "strong_bearish"
    rt, meta = step_gate(rt, f, score, cfg)
    assert rt.state == "neutral"
    assert "two_closes_above_ema20" in (meta["exit_reason"] or "")


def test_two_bars_ema9_ge_ema20_exit() -> None:
    cfg = DirectionGateConfig(variant="B1", min_hold_bars=2)
    rt = GateRuntimeState(state="strong_bearish", age_bars=5, entered_at="t0")
    f = _feat(warmup_ok=True, close_gt_ema20=False, close_lt_ema20=True, ema_9=1.01, ema_20=1.0)
    score = {
        "bearish_entry": False,
        "bullish_entry": False,
        "bullish_required_ok": False,
        "bearish_required_ok": False,
        "bullish_confirm_count": 0,
        "bearish_confirm_count": 0,
        "bullish_need": 2,
        "bearish_need": 2,
    }
    rt, _ = step_gate(rt, f, score, cfg)
    rt, meta = step_gate(rt, f, score, cfg)
    assert rt.state == "neutral"
    assert "ema9_ge_ema20_two_bars" in (meta["exit_reason"] or "")


def test_hl_break_above_lh_exit() -> None:
    cfg = DirectionGateConfig(variant="B1", min_hold_bars=2)
    rt = GateRuntimeState(
        state="strong_bearish",
        age_bars=5,
        entered_at="t0",
        last_lower_high_price=1.0,
    )
    f = _feat(
        warmup_ok=True,
        higher_low=True,
        close=1.01,
        close_gt_ema20=False,
        close_lt_ema20=True,
        ema_9=0.9,
        ema_20=1.0,
    )
    score = {
        "bearish_entry": False,
        "bullish_entry": False,
        "bullish_required_ok": False,
        "bearish_required_ok": False,
        "bullish_confirm_count": 0,
        "bearish_confirm_count": 0,
        "bullish_need": 2,
        "bearish_need": 2,
    }
    rt, meta = step_gate(rt, f, score, cfg)
    assert rt.state == "neutral"
    assert "higher_low_break_above_lh" in (meta["exit_reason"] or "")


def test_bullish_mirror_entry() -> None:
    f = _bullish_trend_bits(_feat())
    f.higher_high = True
    f.higher_low = True
    f.break_above_swing_high = True
    assert evaluate_entry_scores(f, DirectionGateConfig(variant="B3"))["bullish_entry"] is True
    assert evaluate_entry_scores(f, DirectionGateConfig(variant="B1"))["bullish_entry"] is True


def test_sideways_stays_neutral() -> None:
    cfg = DirectionGateConfig(variant="B1")
    rt = GateRuntimeState(state="neutral")
    f = _feat(
        warmup_ok=True,
        close_lt_ema20=False,
        close_gt_ema20=False,
        ema9_lt_ema20=False,
        ema9_gt_ema20=False,
        ema20_slope_neg=False,
        ema20_slope_pos=False,
    )
    score = evaluate_entry_scores(f, cfg)
    rt, _ = step_gate(rt, f, score, cfg)
    assert rt.state == "neutral"
    assert score["bearish_entry"] is False


def test_missing_ema200_no_crash() -> None:
    f = _bearish_trend_bits(_feat(ema_200=None))
    f.lower_high = True
    f.lower_low = True
    f.break_below_swing_low = True
    for v in ("B1", "B2", "B3"):
        evaluate_entry_scores(f, DirectionGateConfig(variant=v))  # type: ignore[arg-type]


def test_outcomes_do_not_affect_gate_assert() -> None:
    gate = pd.DataFrame({"direction_gate_state": ["neutral"], "close": [1.0]})
    outcomes = pd.DataFrame({"setup_id": ["x"], "mfe": [1.0], "mae": [2.0]})
    assert_outcomes_do_not_affect_gate(gate, outcomes)
    bad = gate.copy()
    bad["mfe"] = 1.0
    with pytest.raises(AssertionError):
        assert_outcomes_do_not_affect_gate(bad, outcomes)


def test_identical_inputs_identical_states() -> None:
    cfg = DirectionGateConfig(variant="B1")
    f = _bearish_trend_bits(_feat())
    f.lower_high = True
    f.break_below_swing_low = True
    f.break_prior_day_low = True
    s1 = evaluate_entry_scores(f, cfg)
    s2 = evaluate_entry_scores(f, cfg)
    assert s1 == s2
    r1, _ = step_gate(GateRuntimeState(state="neutral"), f, s1, cfg)
    r2, _ = step_gate(GateRuntimeState(state="neutral"), f, s2, cfg)
    assert r1.state == r2.state


def test_would_block_sides() -> None:
    assert would_block("strong_bearish", "long") is True
    assert would_block("strong_bearish", "short") is False
    assert would_block("strong_bullish", "short") is True
    assert would_block("neutral", "long") is False


def test_warmup_unavailable() -> None:
    cfg = DirectionGateConfig(variant="B1")
    rt = GateRuntimeState(state="neutral")
    f = _bearish_trend_bits(_feat(warmup_ok=False))
    score = evaluate_entry_scores(f, cfg)
    assert score["bearish_entry"] is False
    rt, _ = step_gate(rt, f, score, cfg)
    assert rt.state == "unavailable"

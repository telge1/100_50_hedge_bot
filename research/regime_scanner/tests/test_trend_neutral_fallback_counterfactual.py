"""Unit tests for Phase C2B2B topping/bottoming → neutral fallback."""

from __future__ import annotations

import pandas as pd
import pytest

from research.regime_scanner.trend_state_machine import (
    TrendRuntime,
    default_trend_state_config,
    evaluate_neutral_fallback,
    trend_state_config_c2b,
)


def _cfg_on(*, age_min: int = 48):
    return trend_state_config_c2b(
        "strict",
        weakening_mode="strict",
        turning_window_bars=24,
        neutral_fallback_mode="on",
        neutral_fallback_min_age_bars=age_min,
    )


def _rt(state: str, *, age: int = 48, htf: str = "neutral") -> TrendRuntime:
    rt = TrendRuntime()
    rt.state = state  # type: ignore[assignment]
    rt.age_5m_bars = age
    rt.unavailable_reason = None
    rt.structure_15m.current_structure_bias = htf  # type: ignore[assignment]
    rt.consecutive_bearish_closes = 0
    rt.consecutive_bullish_closes = 0
    return rt


def _flat_row() -> dict:
    return {
        "close": 1.0,
        "ema_9": 1.0,
        "ema_20": 1.0,
        "di_spread": 0.0,
        "adx": 10.0,
        "ema_9_slope_3_pct": 0.0,
        "ema_20_slope_3_pct": 0.0,
    }


def test_default_neutral_fallback_off() -> None:
    d = default_trend_state_config()
    assert d.turning_neutral_fallback_mode == "off"
    c2 = trend_state_config_c2b("strict")
    assert c2.turning_neutral_fallback_mode == "off"


def test_only_topping_bottoming_can_fallback() -> None:
    cfg = _cfg_on()
    for st in ("early_bearish", "early_bullish", "strong_bearish", "neutral", "bullish_weakening"):
        rt = _rt(st, age=100)
        neu, reasons = evaluate_neutral_fallback(rt, types=set(), row=_flat_row(), cfg=cfg)
        assert neu is None
        assert "neutral_fallback_wrong_state" in reasons


def test_configs_off_share_same_transition_path_flag() -> None:
    """Mode off must keep evaluate_neutral_fallback inert (baseline bit-compat)."""
    off = trend_state_config_c2b("strict", neutral_fallback_mode="off")
    on = trend_state_config_c2b("strict", neutral_fallback_mode="on")
    assert off.turning_neutral_fallback_mode == "off"
    assert on.turning_neutral_fallback_mode == "on"
    assert off.weakening_multi_bar_mode == on.weakening_multi_bar_mode == "strict"
    assert off.turning_multi_bar_mode == on.turning_multi_bar_mode == "strict"


def test_age_below_48_blocks() -> None:
    cfg = _cfg_on(age_min=48)
    rt = _rt("topping", age=47, htf="neutral")
    neu, reasons = evaluate_neutral_fallback(rt, types=set(), row=_flat_row(), cfg=cfg)
    assert neu is None
    assert any("age<" in r for r in reasons)


def test_min_hold_blocks() -> None:
    cfg = _cfg_on()
    rt = _rt("topping", age=100, htf="neutral")
    rt.age_5m_bars = 0  # below min_hold of topping (3) — wait age 0 also fails age gate first
    # Use age>=48 but need min_hold: min_hold uses age_5m_bars vs min_hold_bars
    # topping min_hold=3, so age=48 satisfies min_hold. Force age_5m_bars=1 with patched check:
    rt.age_5m_bars = 1
    # Still fails age<48 first. Override min_age to 0 for this test of min_hold only:
    cfg2 = _cfg_on(age_min=0)
    rt.age_5m_bars = 1
    neu, reasons = evaluate_neutral_fallback(rt, types=set(), row=_flat_row(), cfg=cfg2)
    assert neu is None
    assert "min_hold_topping" in reasons


def test_htf_bullish_or_bearish_blocks_neutral_unknown_ok() -> None:
    cfg = _cfg_on()
    for bad in ("bullish", "bearish"):
        rt = _rt("topping", age=60, htf=bad)
        neu, reasons = evaluate_neutral_fallback(rt, types=set(), row=_flat_row(), cfg=cfg)
        assert neu is None
        assert any("htf_15m" in r for r in reasons)
    for ok in ("neutral", "unknown"):
        rt = _rt("topping", age=60, htf=ok)
        neu, reasons = evaluate_neutral_fallback(rt, types=set(), row=_flat_row(), cfg=cfg)
        assert neu == "neutral"
        assert "turning_neutral_fallback" in reasons


def test_same_bar_bos_choch_blocks() -> None:
    cfg = _cfg_on()
    rt = _rt("topping", age=60, htf="neutral")
    neu, _ = evaluate_neutral_fallback(rt, types={"bearish_choch"}, row=_flat_row(), cfg=cfg)
    assert neu is None
    rt2 = _rt("bottoming", age=60, htf="neutral")
    neu2, _ = evaluate_neutral_fallback(rt2, types={"bullish_bos"}, row=_flat_row(), cfg=cfg)
    assert neu2 is None


def test_stored_turning_evidence_blocks() -> None:
    cfg = _cfg_on()
    rt = _rt("topping", age=60, htf="neutral")
    rt.turning_evidence_keys["bearish_choch"] = "x"
    neu, reasons = evaluate_neutral_fallback(rt, types=set(), row=_flat_row(), cfg=cfg)
    assert neu is None
    assert "neutral_fallback_bearish_hard_evidence" in reasons


def test_impulse_blocks() -> None:
    cfg = _cfg_on()
    rt = _rt("topping", age=60, htf="neutral")
    rt.consecutive_bearish_closes = 2
    neu, reasons = evaluate_neutral_fallback(rt, types=set(), row=_flat_row(), cfg=cfg)
    assert neu is None
    assert "neutral_fallback_bearish_impulse" in reasons


def test_continuation_same_bar_blocks() -> None:
    cfg = _cfg_on()
    rt = _rt("topping", age=60, htf="neutral")
    neu, reasons = evaluate_neutral_fallback(rt, types={"higher_high"}, row=_flat_row(), cfg=cfg)
    assert neu is None
    assert "neutral_fallback_bullish_continuation" in reasons
    rt2 = _rt("bottoming", age=60, htf="neutral")
    neu2, reasons2 = evaluate_neutral_fallback(rt2, types={"lower_low"}, row=_flat_row(), cfg=cfg)
    assert neu2 is None
    assert "neutral_fallback_bearish_continuation" in reasons2


def test_mode_off_no_fallback() -> None:
    cfg = trend_state_config_c2b("strict", neutral_fallback_mode="off")
    rt = _rt("topping", age=100, htf="neutral")
    neu, reasons = evaluate_neutral_fallback(rt, types=set(), row=_flat_row(), cfg=cfg)
    assert neu is None
    assert "neutral_fallback_mode_off" in reasons


def test_bottoming_mirror_allows() -> None:
    cfg = _cfg_on()
    rt = _rt("bottoming", age=55, htf="unknown")
    neu, reasons = evaluate_neutral_fallback(rt, types=set(), row=_flat_row(), cfg=cfg)
    assert neu == "neutral"
    assert "bottoming_neutral_fallback" in reasons

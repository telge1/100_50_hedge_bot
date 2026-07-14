"""Tests for Phase C2B1 topping/bottoming multi-bar turning exits."""

from __future__ import annotations

import inspect
from dataclasses import replace

import pandas as pd
import pytest

from research.regime_scanner.trend_state_machine import (
    TrendRuntime,
    clear_turning_evidence,
    default_trend_state_config,
    multi_bar_turning_exit,
    trend_state_config_c1,
    trend_state_config_c2b,
    update_turning_evidence,
)
from research.regime_scanner.trend_structure import StructureEvent


def _ev(etype: str, t: str, level: float = 1.0) -> StructureEvent:
    return StructureEvent(
        event_type=etype,
        timeframe="5m",
        event_time=pd.Timestamp(t, tz="UTC"),
        level=level,
        reference_pivot_time=None,
        reference_pivot_price=None,
        direction="bearish" if "bear" in etype or etype == "lower_high" else "bullish",
        reason_codes=("test",),
    )


def _bear_row() -> dict:
    return {
        "close": 0.95,
        "ema_9": 0.96,
        "ema_20": 1.0,
        "di_spread": -8.0,
        "adx": 25.0,
        "ema_9_slope_3_pct": -0.1,
        "ema_20_slope_3_pct": -0.05,
        "decision_time": "2026-01-01T01:00:00+00:00",
    }


def test_default_turning_mode_off_and_c1_default_unchanged() -> None:
    d = default_trend_state_config()
    assert d.turning_multi_bar_mode == "off"
    assert d.weakening_multi_bar_mode == "off"
    c1 = trend_state_config_c1("strict")
    assert c1.weakening_multi_bar_mode == "strict"
    assert c1.turning_multi_bar_mode == "off"


def test_invalid_mode_raises() -> None:
    with pytest.raises(ValueError):
        trend_state_config_c2b("medium")  # type: ignore[arg-type]


def test_no_march_hardcode_in_sm() -> None:
    import research.regime_scanner.trend_state_machine as m

    assert "2026-03-06" not in inspect.getsource(m)
    assert "APTUSDT" not in inspect.getsource(m)


def test_persisted_choch_plus_lh_and_impulse_exits_topping_loose() -> None:
    rt = TrendRuntime()
    rt.state = "topping"
    rt.age_5m_bars = 5
    rt.unavailable_reason = None
    rt.consecutive_bearish_closes = 2
    rt.structure_5m.last_high_label = "lower_high"
    rt.structure_5m.last_choch = _ev("bearish_choch", "2026-01-01T00:30:00+00:00")
    cfg = trend_state_config_c2b("loose", turning_window_bars=24)
    dt = pd.Timestamp("2026-01-01T01:00:00+00:00")
    update_turning_evidence(rt, events=[], cfg=cfg, decision_time=dt)
    assert "bearish_choch" in rt.turning_evidence_keys
    st, reasons = multi_bar_turning_exit(
        rt, types=set(), row=_bear_row(), cfg=cfg, decision_time=dt
    )
    assert st == "early_bearish"
    assert "turning_multi_bar_early_bearish" in reasons
    assert any(r.startswith("mode:loose") for r in reasons)


def test_persisted_bullish_choch_plus_hl_exits_bottoming() -> None:
    rt = TrendRuntime()
    rt.state = "bottoming"
    rt.age_5m_bars = 4
    rt.unavailable_reason = None
    rt.consecutive_bullish_closes = 2
    rt.structure_5m.last_low_label = "higher_low"
    rt.structure_5m.last_choch = _ev("bullish_choch", "2026-01-01T00:40:00+00:00")
    cfg = trend_state_config_c2b("loose", turning_window_bars=24)
    dt = pd.Timestamp("2026-01-01T01:00:00+00:00")
    update_turning_evidence(rt, events=[], cfg=cfg, decision_time=dt)
    row = {
        "close": 1.05,
        "ema_9": 1.04,
        "ema_20": 1.0,
        "di_spread": 8.0,
        "adx": 25.0,
        "ema_9_slope_3_pct": 0.1,
        "ema_20_slope_3_pct": 0.05,
    }
    st, reasons = multi_bar_turning_exit(rt, types=set(), row=row, cfg=cfg, decision_time=dt)
    assert st == "early_bullish"
    assert "turning_multi_bar_early_bullish" in reasons


def test_same_event_not_double_counted() -> None:
    rt = TrendRuntime()
    rt.state = "topping"
    rt.age_5m_bars = 2
    cfg = trend_state_config_c2b("loose")
    dt = pd.Timestamp("2026-01-01T01:00:00+00:00")
    e = _ev("bearish_choch", "2026-01-01T00:50:00+00:00")
    update_turning_evidence(rt, events=[e], cfg=cfg, decision_time=dt)
    update_turning_evidence(rt, events=[e], cfg=cfg, decision_time=dt)
    assert list(rt.turning_evidence_keys.keys()) == ["bearish_choch"]


def test_bull_and_bear_evidence_not_mixed() -> None:
    rt = TrendRuntime()
    rt.state = "topping"
    rt.age_5m_bars = 3
    cfg = trend_state_config_c2b("loose")
    dt = pd.Timestamp("2026-01-01T01:00:00+00:00")
    update_turning_evidence(
        rt,
        events=[_ev("bullish_choch", "2026-01-01T00:50:00+00:00"), _ev("bearish_bos", "2026-01-01T00:55:00+00:00")],
        cfg=cfg,
        decision_time=dt,
    )
    assert "bullish_choch" not in rt.turning_evidence_keys
    assert "bearish_bos" in rt.turning_evidence_keys


def test_evidence_expires_after_window() -> None:
    rt = TrendRuntime()
    rt.state = "topping"
    rt.age_5m_bars = 0
    cfg = replace(trend_state_config_c2b("loose"), turning_evidence_window_bars=5)
    dt0 = pd.Timestamp("2026-01-01T00:00:00+00:00")
    update_turning_evidence(
        rt, events=[_ev("bearish_choch", "2026-01-01T00:00:00+00:00")], cfg=cfg, decision_time=dt0
    )
    rt.age_5m_bars = 10
    update_turning_evidence(rt, events=[], cfg=cfg, decision_time=dt0 + pd.Timedelta(minutes=50))
    assert "bearish_choch" not in rt.turning_evidence_keys


def test_continuation_invalidates_evidence() -> None:
    rt = TrendRuntime()
    rt.state = "topping"
    rt.age_5m_bars = 3
    cfg = trend_state_config_c2b("loose")
    dt = pd.Timestamp("2026-01-01T01:00:00+00:00")
    update_turning_evidence(
        rt, events=[_ev("bearish_choch", "2026-01-01T00:50:00+00:00")], cfg=cfg, decision_time=dt
    )
    notes = update_turning_evidence(
        rt, events=[_ev("higher_high", "2026-01-01T01:00:00+00:00")], cfg=cfg, decision_time=dt
    )
    assert not rt.turning_evidence_keys
    assert "turning_evidence_reset_continuation" in notes


def test_mode_off_no_exit() -> None:
    rt = TrendRuntime()
    rt.state = "topping"
    rt.age_5m_bars = 5
    rt.consecutive_bearish_closes = 3
    rt.structure_5m.last_high_label = "lower_high"
    rt.turning_evidence_keys = {"bearish_choch": "x", "lower_high": "y"}
    cfg = default_trend_state_config()
    st, _ = multi_bar_turning_exit(
        rt,
        types=set(),
        row=_bear_row(),
        cfg=cfg,
        decision_time=pd.Timestamp("2026-01-01T01:00:00+00:00"),
    )
    assert st is None


def test_strict_needs_extra_confirm() -> None:
    rt = TrendRuntime()
    rt.state = "topping"
    rt.age_5m_bars = 6
    rt.unavailable_reason = None
    rt.consecutive_bearish_closes = 2
    rt.structure_5m.last_high_label = "lower_high"
    rt.structure_15m.current_structure_bias = "bullish"
    cfg = trend_state_config_c2b("strict")
    dt = pd.Timestamp("2026-01-01T01:00:00+00:00")
    rt.turning_evidence_keys = {"bearish_choch": "a", "lower_high": "b"}
    rt.turning_evidence_seen_age = {"bearish_choch": 1, "lower_high": 2}
    # Flat indicators → impulse only via closes; no HTF bearish → blocked in strict
    flat = {
        "close": 1.02,
        "ema_9": 1.01,
        "ema_20": 1.0,
        "di_spread": 0.0,
        "adx": 10.0,
        "ema_9_slope_3_pct": 0.0,
        "ema_20_slope_3_pct": 0.0,
    }
    st, reasons = multi_bar_turning_exit(rt, types=set(), row=flat, cfg=cfg, decision_time=dt)
    assert st is None
    assert "turning_strict_need_htf_or_indicator" in reasons

    rt.structure_15m.current_structure_bias = "bearish"
    st2, reasons2 = multi_bar_turning_exit(rt, types=set(), row=flat, cfg=cfg, decision_time=dt)
    assert st2 == "early_bearish"
    assert "mode:strict" in reasons2


def test_clear_on_helper_and_state_fields_present() -> None:
    rt = TrendRuntime()
    rt.turning_evidence_keys["x"] = "y"
    rt.turning_evidence_seen_age["x"] = 1
    clear_turning_evidence(rt)
    assert not rt.turning_evidence_keys
    assert hasattr(rt, "turning_evidence_keys")
    assert hasattr(rt, "turning_evidence_seen_age")


def test_audit_refuses_prior_result_dirs() -> None:
    from pathlib import Path

    from research.regime_scanner.trend_topping_bottoming_multibar_audit import assert_safe_output_dir

    with pytest.raises(ValueError):
        assert_safe_output_dir(Path("research/regime_scanner/results_trend_topping_bottoming_phase_c2a"))


def test_c2b_config_keeps_c1_strict() -> None:
    cfg = trend_state_config_c2b("loose")
    assert cfg.weakening_multi_bar_mode == "strict"
    assert cfg.turning_multi_bar_mode == "loose"
    assert cfg.turning_evidence_window_bars == 24

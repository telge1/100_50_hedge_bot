"""Tests for C3.3B clean-regime state machine and Pine parity."""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from research.regime_scanner.trend_detector_clean_regime import (
    ALLOWED_TRANSITIONS,
    CLEAN_STATE_CODE,
    CleanRegimeConfig,
    CleanRuntimeState,
    apply_clean_regime,
    build_rule_spec,
    config_hash,
    pine_rule_hash,
    python_rule_hash,
    rule_spec_hash,
    step_clean_regime_state,
)
from research.regime_scanner.trend_detector_clean_regime_audit import (
    run_synthetic_parity,
    synthetic_parity_sequence,
)
from research.regime_scanner.trend_detector_clean_regime_pine import (
    CLEAN_PINE_NAME,
    build_clean_regime_pine_script,
    write_clean_regime_pines,
)
from research.regime_scanner.trend_pine_export import AUDIT_ANCHOR_PLOT, validate_pine_script
from research.regime_scanner.trend_regime_classification_audit import C2_BASELINE_HASH


def _feat(**kwargs: object) -> dict[str, object]:
    base: dict[str, object] = {
        "raw_research_state": "neutral",
        "building_bull": False,
        "building_bear": False,
        "confirmed_bull": False,
        "confirmed_bear": False,
        "hold_bull_confirmed": False,
        "hold_bear_confirmed": False,
        "weaken_bull": False,
        "weaken_bear": False,
        "lose_bull": False,
        "lose_bear": False,
        "di_bull": False,
        "di_bear": False,
        "di_direction": 0,
        "di_diff": 0.0,
        "adx": 20.0,
        "adx_rising": False,
        "adx_slope_3": 0.0,
        "adx_slope_5": 0.0,
        "ema_order_direction": 0,
        "ema_joint_slope_direction": 0,
        "band_expanding": False,
        "atr_relevant": False,
        "bullish_component_count": 0,
        "bearish_component_count": 0,
        "net_research_score": 0,
    }
    base.update(kwargs)
    return base


def test_rule_spec_is_source_of_truth_and_hashes_match() -> None:
    for variant in ("light", "medium", "strong"):
        cfg = CleanRegimeConfig.for_variant(variant)
        spec = build_rule_spec(cfg)
        assert spec["timing"]["building_confirmation"] == cfg.building_confirmation
        assert spec["timing"]["confirmed_confirmation"] == cfg.confirmed_confirmation
        assert spec["timing"]["min_confirmed_hold"] == cfg.min_confirmed_hold
        assert spec["timing"]["cooldown_bars"] == cfg.cooldown_bars
        assert python_rule_hash(cfg) == pine_rule_hash(cfg) == rule_spec_hash(spec)
        assert config_hash(cfg)
        assert set(spec["allowed_transitions"]) == set(ALLOWED_TRANSITIONS)


def test_variant_presets() -> None:
    light = CleanRegimeConfig.for_variant("light")
    medium = CleanRegimeConfig.for_variant("medium")
    strong = CleanRegimeConfig.for_variant("strong")
    assert light.building_confirmation == 2 and light.confirmed_confirmation == 2
    assert light.min_confirmed_hold == 3 and light.cooldown_bars == 1
    assert medium.confirmed_confirmation == 3 and medium.min_confirmed_hold == 4
    assert strong.building_confirmation == 3 and strong.opposite_confirmation == 4
    assert strong.min_confirmed_hold == 5


def test_single_candidate_candle_suppressed() -> None:
    cfg = CleanRegimeConfig.for_variant("medium")
    rt = CleanRuntimeState()
    state, rt, diag = step_clean_regime_state(
        "neutral",
        _feat(building_bull=True, di_bull=True, net_research_score=3, bullish_component_count=6),
        rt,
        cfg,
    )
    assert state == "neutral"
    assert diag["transition_reason"] == "awaiting_confirmation"
    assert rt.candidate_count == 1


def test_confirmed_transition_to_building_and_confirmed() -> None:
    cfg = CleanRegimeConfig.for_variant("light")
    rt = CleanRuntimeState()
    prev = "neutral"
    for _ in range(cfg.building_confirmation):
        prev, rt, _ = step_clean_regime_state(
            prev,
            _feat(building_bull=True, di_bull=True, net_research_score=3, bullish_component_count=6),
            rt,
            cfg,
        )
    assert prev == "bullish_building"
    for _ in range(cfg.min_building_hold):
        prev, rt, _ = step_clean_regime_state(
            prev,
            _feat(
                building_bull=True,
                hold_bull_confirmed=True,
                di_bull=True,
                net_research_score=2,
                bullish_component_count=5,
            ),
            rt,
            cfg,
        )
    for _ in range(cfg.confirmed_confirmation):
        prev, rt, _ = step_clean_regime_state(
            prev,
            _feat(
                confirmed_bull=True,
                building_bull=True,
                hold_bull_confirmed=True,
                di_bull=True,
                net_research_score=5,
                bullish_component_count=8,
            ),
            rt,
            cfg,
        )
    assert prev == "bullish_confirmed"


def test_single_weak_candle_does_not_exit_confirmed() -> None:
    cfg = CleanRegimeConfig.for_variant("light")
    rt = CleanRuntimeState(
        clean_state="bullish_confirmed", state_age_bars=10, bars_since_transition=10
    )
    state, rt, diag = step_clean_regime_state(
        "bullish_confirmed",
        _feat(
            hold_bull_confirmed=True,
            di_bull=True,
            weaken_bull=True,
            net_research_score=0,
            bullish_component_count=4,
            bearish_component_count=2,
        ),
        rt,
        cfg,
    )
    assert state == "bullish_confirmed"
    assert diag["transition_reason"] == "hold"


def test_no_direct_confirmed_flip() -> None:
    cfg = CleanRegimeConfig.for_variant("medium")
    rt = CleanRuntimeState(
        clean_state="bullish_confirmed", state_age_bars=20, bars_since_transition=20
    )
    state, rt, diag = step_clean_regime_state(
        "bullish_confirmed",
        _feat(
            lose_bull=True,
            building_bear=True,
            confirmed_bear=True,
            di_bear=True,
            net_research_score=-5,
            bearish_component_count=9,
        ),
        rt,
        cfg,
    )
    assert state in {"bullish_confirmed", "bullish_building"}
    assert state != "bearish_confirmed"
    assert diag.get("desired_state") != "bearish_confirmed"


def test_direction_and_strength_codes() -> None:
    assert CLEAN_STATE_CODE["neutral"] == 0
    assert CLEAN_STATE_CODE["bullish_building"] == 1
    assert CLEAN_STATE_CODE["bullish_confirmed"] == 2
    assert CLEAN_STATE_CODE["bearish_building"] == -1
    assert CLEAN_STATE_CODE["bearish_confirmed"] == -2


def test_non_repaint_closed_bars_immutable() -> None:
    cfg = CleanRegimeConfig.for_variant("medium")
    rows = []
    for i in range(10):
        rows.append(
            {
                "bar_index": i,
                "timestamp": pd.Timestamp("2026-03-01", tz="UTC") + pd.Timedelta(minutes=30 * i),
                "decision_time": pd.Timestamp("2026-03-01", tz="UTC")
                + pd.Timedelta(minutes=30 * (i + 1)),
                "symbol": "APTUSDT",
                "timeframe": "30m",
                "research_state": "early_bullish" if i >= 2 else "neutral",
                "di_bull": i >= 2,
                "di_bear": False,
                "ema_bull_order": i >= 5,
                "ema_bear_order": False,
                "adx_rising": True,
                "adx_falling": False,
                "adx_confirm": True,
                "band_expand": i >= 5,
                "band_compress": False,
                "di_shrinking": False,
                "joint_rising": i >= 4,
                "joint_falling": False,
                "move_relevant": True,
                "bullish_component_count": 6 if i >= 2 else 0,
                "bearish_component_count": 1,
                "net_score": 3 if i >= 2 else 0,
                "di_spread": 2.0,
                "adx_14": 22.0,
                "adx_slope_lin_3": 0.2,
                "adx_slope_lin_5": 0.2,
            }
        )
    frame = pd.DataFrame(rows)
    a = apply_clean_regime(frame, cfg)
    b = apply_clean_regime(frame, cfg)
    assert a["clean_regime_state"].tolist() == b["clean_regime_state"].tolist()
    more = pd.concat([frame, frame.iloc[[-1]].assign(bar_index=10)], ignore_index=True)
    c = apply_clean_regime(more, cfg)
    assert c["clean_regime_state"].iloc[: len(a)].tolist() == a["clean_regime_state"].tolist()


def test_retro_columns_do_not_affect_state() -> None:
    cfg = CleanRegimeConfig.for_variant("light")
    rt = CleanRuntimeState()
    feat = _feat(building_bull=True, di_bull=True, net_research_score=3, bullish_component_count=6)
    feat_retro = dict(feat)
    feat_retro["outcome_class_retro"] = "clean_success"
    feat_retro["paired_ema_lag_bars"] = 2
    s1, _, d1 = step_clean_regime_state("neutral", feat, rt, cfg)
    rt2 = CleanRuntimeState()
    s2, _, d2 = step_clean_regime_state("neutral", feat_retro, rt2, cfg)
    assert s1 == s2
    assert d1["transition_reason"] == d2["transition_reason"]


def test_pine_header_and_no_strategy(tmp_path: Path) -> None:
    text = build_clean_regime_pine_script(fixed_variant="medium")
    validate_pine_script(text)
    assert text.startswith("//@version=6\n")
    assert text.count("indicator(") == 1
    assert text.count(AUDIT_ANCHOR_PLOT) == 1
    assert re.search(r"(?m)^strategy\(", text) is None
    assert 'bgcolor(cleanState == "bullish_confirmed"' in text
    bg = text.split("Background ONLY")[1].split("plot(")[0]
    assert "cleanState ==" in bg
    assert "rawState" not in bg
    meta = write_clean_regime_pines(tmp_path)
    assert (tmp_path / CLEAN_PINE_NAME).is_file()
    assert meta["python_rule_hash"] == meta["pine_rule_hash"]


def test_pine_embeds_same_thresholds() -> None:
    cfg = CleanRegimeConfig.for_variant("strong")
    text = build_clean_regime_pine_script(cfg=cfg, fixed_variant="strong")
    assert f"buildingConf = {cfg.building_confirmation}" in text
    assert f"confirmedConf = {cfg.confirmed_confirmation}" in text
    assert f"minConfirmedHold = {cfg.min_confirmed_hold}" in text
    assert f"cooldownBars = {cfg.cooldown_bars}" in text
    assert f"adxConfirmMin = {cfg.adx_level_confirmation_min}" in text


def test_synthetic_parity_and_state_codes() -> None:
    cfg = CleanRegimeConfig.for_variant("medium")
    rows = run_synthetic_parity(cfg)
    assert len(rows) == len(synthetic_parity_sequence(cfg))
    states = [r["expected_python_clean_state"] for r in rows]
    assert "neutral" in states
    assert any(s == "bullish_building" for s in states)
    for a, b in zip(states, states[1:]):
        assert not (a == "bullish_confirmed" and b == "bearish_confirmed")
        assert not (a == "bearish_confirmed" and b == "bullish_confirmed")
    for r in rows:
        assert r["clean_state_code"] == CLEAN_STATE_CODE[r["expected_python_clean_state"]]
        assert r["pine_expected_same"] is True


def test_baseline_hash_unchanged() -> None:
    assert C2_BASELINE_HASH == (
        "702ba3e62976aeae879d053a03f64eaba06771beac367248dcfca8d4ebc4ec61"
    )


def test_min_hold_and_cooldown_suppress() -> None:
    cfg = CleanRegimeConfig(
        variant="test",
        building_confirmation=1,
        confirmed_confirmation=1,
        neutral_confirmation=1,
        opposite_confirmation=1,
        min_building_hold=5,
        min_confirmed_hold=5,
        cooldown_bars=3,
    )
    rt = CleanRuntimeState(
        clean_state="bullish_building", state_age_bars=1, bars_since_transition=1
    )
    state, rt, diag = step_clean_regime_state(
        "bullish_building",
        _feat(lose_bull=True),
        rt,
        cfg,
    )
    assert state == "bullish_building"
    assert diag["transition_reason"] == "suppressed_min_hold"

"""Tests for Phase C3.2B-D indicator-pattern audit."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import research.regime_scanner.trend_indicator_ablation as tia
import research.regime_scanner.trend_regime_classifier as trc
from research.regime_scanner.trend_audit_shared_replay import PreparedBar
from research.regime_scanner.trend_indicator_ablation import (
    IndicatorPatternConfig,
    apply_indicator_gate,
    compute_adx_di_scores,
    compute_ema_band_scores,
    config_for_variant,
    extract_breakout_events,
    extract_trend_follow_events,
    replay_indicator_variant,
    VARIANT_BASELINE,
    VARIANT_EMA,
    VARIANT_EMA_ADX_DI,
)
from research.regime_scanner.trend_indicator_pattern_audit import _enrich_event_outcomes
from research.regime_scanner.trend_pine_export import build_pine_header, validate_pine_script
from research.regime_scanner.trend_regime_classifier import config_c3, replay_regime_variant
from research.regime_scanner.trend_state_forward_outcome_audit import compute_horizon_outcome
from research.regime_scanner.trend_structure import MarketStructureState


def _ind_row(
    *,
    ema9: float,
    ema20: float,
    ema59: float,
    ema200: float,
    atr: float,
    adx: float,
    plus_di: float,
    minus_di: float,
    cross_count: float = 0.0,
    compression: float = 0.0,
    expansion: float = 0.0,
    slope9: float = 0.0,
    slope20: float = 0.0,
    slope59: float = 0.0,
    slope200: float = 0.0,
    ordered_bull: bool = False,
    ordered_bear: bool = False,
    close: float | None = None,
) -> dict[str, float | bool]:
    close = float(close if close is not None else ema9)
    row: dict[str, float | bool] = {
        "features_ready": True,
        "ema_9": ema9,
        "ema_20": ema20,
        "ema_59": ema59,
        "ema_200": ema200,
        "atr_14": atr,
        "adx_14": adx,
        "plus_di_14": plus_di,
        "minus_di_14": minus_di,
        "ema_9_20_spread_atr": (ema9 - ema20) / atr,
        "ema_fast_cross_count_24": cross_count,
        "ema_fast_compression_score": compression,
        "ema_fast_expansion_score": expansion,
        "ema_bullish_ordered": ordered_bull,
        "ema_bearish_ordered": ordered_bear,
        "price_above_ema_200": close > ema200,
        "price_below_ema_200": close < ema200,
        "close_to_ema_200_atr": (close - ema200) / atr,
        "ema_9_slope_3_atr": slope9,
        "ema_20_slope_3_atr": slope20,
        "ema_59_slope_3_atr": slope59,
        "ema_200_slope_3_atr": slope200,
        "close": close,
    }
    return row


def _breakout_row(
    *,
    ts: str,
    bar_index: int,
    close: float,
    high: float,
    low: float,
    range_high: float,
    range_low: float,
    state: str = "range_sideways",
    c31_state: str = "range_sideways",
    parent_trend: str | None = None,
    breakout_up_score: float = 0.0,
    breakout_down_score: float = 0.0,
    breakout_up_structure_confirmed: bool = False,
    breakout_down_structure_confirmed: bool = False,
) -> dict[str, object]:
    atr = max(1.0, (high - low) / 2.0)
    return {
        "decision_time": ts,
        "bar_index": bar_index,
        "state": state,
        "previous_state": state,
        "c31_state": c31_state,
        "parent_trend": parent_trend,
        "features_ready": True,
        "indicator_version": "c3.2a_v1",
        "variant_id": VARIANT_EMA,
        "mode": "ema",
        "close": close,
        "high": high,
        "low": low,
        "atr": atr,
        "range_high": range_high,
        "range_low": range_low,
        "range_mid": (range_high + range_low) / 2.0,
        "range_width_atr": (range_high - range_low) / atr,
        "range_score": 0.75,
        "range_de": 0.2,
        "range_net_move_atr": 0.1,
        "range_box_efficiency": 0.9,
        "range_bound_drift": 0.1,
        "ema_state": "ema_bullish_expanding" if close >= range_high else "ema_range_like",
        "ema_range_score": 0.25,
        "ema_bullish_trend_score": 0.8,
        "ema_bearish_trend_score": 0.1,
        "ema_bullish_breakout_score": breakout_up_score,
        "ema_bearish_breakout_score": breakout_down_score,
        "ema_pullback_support_score": 0.4,
        "ema_bullish_ordered_score": 0.2,
        "ema_bearish_ordered_score": 0.2,
        "ema_expansion_score": 0.7,
        "ema_compression_score": 0.2,
        "adx_dominant_di": "plus" if breakout_up_score >= breakout_down_score else "minus",
        "di_bullish_confirmation": 0.9,
        "di_bearish_confirmation": 0.1,
        "di_neutral": 0.0,
        "adx_weak": 0.0,
        "adx_strengthening": 0.0,
        "adx_strong": 0.7,
        "adx_falling": 0.0,
        "di_component_bull": 0.8,
        "di_component_bear": 0.1,
        "adx_component": 0.6,
        "breakout_up_score": breakout_up_score,
        "breakout_down_score": breakout_down_score,
        "breakout_up_price_score": breakout_up_score,
        "breakout_down_price_score": breakout_down_score,
        "breakout_up_structure_score": 0.7,
        "breakout_down_structure_score": 0.7,
        "breakout_up_ema_score": breakout_up_score,
        "breakout_down_ema_score": breakout_down_score,
        "breakout_up_di_score": 0.5,
        "breakout_down_di_score": 0.5,
        "breakout_up_adx_score": 0.5,
        "breakout_down_adx_score": 0.5,
        "breakout_up_structure_confirmed": breakout_up_structure_confirmed,
        "breakout_down_structure_confirmed": breakout_down_structure_confirmed,
        "breakout_up_breakout_level": range_high + atr * 0.15,
        "breakout_down_breakout_level": range_low - atr * 0.15,
        "breakout_up_outside_bars": 0,
        "breakout_down_outside_bars": 0,
        "breakout_acceptance_bars": 2,
        "breakout_max_confirmation_bars": 12,
        "breakout_ema_confirmation_min": 0.4,
        "breakout_structure_confirmation_required": True,
        "breakout_quick_reentry_bars": 4,
    }


def _prep_bar(index: int, ts: str, close: float) -> PreparedBar:
    row = {
        "decision_time": ts,
        "close": close,
        "high": close + 1.0,
        "low": close - 1.0,
        "atr": 1.0,
    }
    return PreparedBar(
        bar_index=index,
        decision_time=pd.Timestamp(ts, tz="UTC"),
        row=row,
        events_5m=[],
        structure_5m=MarketStructureState(),
        structure_15m=MarketStructureState(),
        structure_30m=MarketStructureState(),
        last_15m_bucket=None,
        last_30m_bucket=None,
        consecutive_bearish_closes=0,
        consecutive_bullish_closes=0,
        bars_since_ll=0,
        bars_since_hh=0,
        scores={"bearish_score": 0.0, "bullish_score": 0.0, "weakening_score": 0.0, "bottoming_score": 0.0},
    )


def _patch_fake_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_build_bar_features(prepared, arrays, *, net_move_window, efficiency_window, overlap_window):
        return SimpleNamespace(
            bar_index=prepared.bar_index,
            net_move_atr=0.0,
            directional_efficiency=0.0,
            overlap_ratio=0.0,
            close=float(prepared.row["close"]),
            high=float(prepared.row["high"]),
            low=float(prepared.row["low"]),
            atr=1.0,
            range_width_atr=1.0,
            range_de=0.1,
            range_net_move_atr=0.1,
            box_efficiency=0.8,
            bound_drift_atr=0.1,
            failed_breakout_count=0.0,
            alternating_score=0.1,
            hh_hl=False,
            lh_ll=False,
            bull_bos=False,
            bear_bos=False,
            bull_choch=False,
            bear_choch=False,
            htf_bias="neutral",
            rolling_high=float(prepared.row["high"]),
            rolling_low=float(prepared.row["low"]),
        )

    def fake_step(rt, feat, cfg):
        rt.state = "range_sideways" if feat.bar_index < 2 else "confirmed_uptrend"
        rt.parent_trend = "none" if feat.bar_index < 2 else "up"
        rt.active_reasons = ["fake"]
        return rt

    def fake_range_score(feat, *, cfg, sustained_bos_up, sustained_bos_down):
        return {"range_score": 0.5, "part_de": 0.1, "part_net": 0.1, "part_box": 0.1, "part_drift": 0.1}

    for mod in (tia, trc):
        monkeypatch.setattr(mod, "build_bar_features", fake_build_bar_features)
        monkeypatch.setattr(mod, "step_regime_classifier", fake_step)
        monkeypatch.setattr(mod, "compute_range_score", fake_range_score)


def test_ema_scores_sideways_range_like() -> None:
    row = _ind_row(
        ema9=100.0,
        ema20=100.02,
        ema59=100.03,
        ema200=100.0,
        atr=2.0,
        adx=12.0,
        plus_di=20.0,
        minus_di=19.0,
        cross_count=4.0,
        compression=0.95,
        expansion=0.05,
        slope9=0.0,
        slope20=0.0,
        slope59=0.0,
        slope200=0.0,
    )
    scores = compute_ema_band_scores(row, config_for_variant(VARIANT_BASELINE))
    assert scores["ema_state"] == "ema_range_like"


def test_ema_scores_expanding_bull_is_expanding() -> None:
    row = _ind_row(
        ema9=110.0,
        ema20=108.0,
        ema59=104.0,
        ema200=100.0,
        atr=2.0,
        adx=28.0,
        plus_di=32.0,
        minus_di=12.0,
        cross_count=0.0,
        compression=0.05,
        expansion=0.95,
        slope9=0.5,
        slope20=0.45,
        slope59=0.35,
        slope200=0.1,
        ordered_bull=False,
        close=111.0,
    )
    scores = compute_ema_band_scores(row, config_for_variant(VARIANT_EMA))
    assert scores["ema_state"] == "ema_bullish_expanding"


def test_config_for_variant_modes() -> None:
    assert config_for_variant(VARIANT_BASELINE).mode == "baseline"
    assert config_for_variant(VARIANT_EMA).mode == "ema"
    assert config_for_variant(VARIANT_EMA_ADX_DI).mode == "ema_adx_di"
    assert config_c3("conservative").variant_id == "C3_A_conservative"


def test_breakout_failed_event_has_breakout_id_and_result() -> None:
    cfg = replace(config_for_variant(VARIANT_EMA), breakout_quick_reentry_bars=0)
    timeline = [
        _breakout_row(
            ts="2026-03-01T00:00:00+00:00",
            bar_index=0,
            close=100.0,
            high=100.5,
            low=99.5,
            range_high=100.0,
            range_low=98.0,
        ),
        _breakout_row(
            ts="2026-03-01T00:05:00+00:00",
            bar_index=1,
            close=101.6,
            high=101.8,
            low=100.9,
            range_high=100.0,
            range_low=98.0,
            breakout_up_score=0.85,
            breakout_up_structure_confirmed=True,
        ),
        _breakout_row(
            ts="2026-03-01T00:10:00+00:00",
            bar_index=2,
            close=99.5,
            high=100.0,
            low=99.0,
            range_high=100.0,
            range_low=98.0,
        ),
    ]
    events = extract_breakout_events(timeline, VARIANT_EMA, "TEST", "5m", cfg)
    assert len(events) == 1
    assert events[0]["breakout_id"] == events[0]["event_id"]
    assert events[0]["lifecycle_outcome"] == "failed"
    assert events[0]["result"] == "failed"


def test_breakout_confirmed_event_and_multi_bar_outside_one_event() -> None:
    cfg = replace(
        config_for_variant(VARIANT_EMA),
        breakout_acceptance_bars=1,
        breakout_ema_confirmation_min=0.2,
        breakout_structure_confirmation_required=False,
        breakout_quick_reentry_bars=0,
    )
    timeline = [
        _breakout_row(
            ts="2026-03-01T00:00:00+00:00",
            bar_index=0,
            close=100.0,
            high=100.5,
            low=99.5,
            range_high=100.0,
            range_low=98.0,
        ),
        _breakout_row(
            ts="2026-03-01T00:05:00+00:00",
            bar_index=1,
            close=101.5,
            high=101.7,
            low=101.0,
            range_high=100.0,
            range_low=98.0,
            breakout_up_score=0.75,
        ),
        _breakout_row(
            ts="2026-03-01T00:10:00+00:00",
            bar_index=2,
            close=101.8,
            high=102.0,
            low=101.2,
            range_high=100.0,
            range_low=98.0,
            breakout_up_score=0.8,
        ),
    ]
    events = extract_breakout_events(timeline, VARIANT_EMA, "TEST", "5m", cfg)
    assert len(events) == 1
    assert events[0]["lifecycle_outcome"] == "confirmed"
    assert events[0]["result"] == "confirmed"
    assert events[0]["confirm_time"] == "2026-03-01T00:10:00+00:00"


def test_wick_pierce_without_close_outside_has_no_attempt() -> None:
    cfg = config_for_variant(VARIANT_EMA)
    timeline = [
        _breakout_row(
            ts="2026-03-01T00:00:00+00:00",
            bar_index=0,
            close=99.8,
            high=101.2,
            low=99.5,
            range_high=100.0,
            range_low=98.0,
        ),
    ]
    assert extract_breakout_events(timeline, VARIANT_EMA, "TEST", "5m", cfg) == []


def test_baseline_gate_passthrough() -> None:
    cfg = config_for_variant(VARIANT_BASELINE)
    row = _ind_row(
        ema9=100.0,
        ema20=100.0,
        ema59=100.0,
        ema200=100.0,
        atr=1.0,
        adx=10.0,
        plus_di=10.0,
        minus_di=10.0,
    )
    new_state, reason = apply_indicator_gate(
        prev_shadow="confirmed_uptrend",
        c31_prev="confirmed_uptrend",
        c31_state="confirmed_downtrend",
        bars_in_shadow=3,
        ind=row,
        cfg=cfg,
        parent_trend="up",
    )
    assert new_state == "confirmed_downtrend"
    assert reason == "baseline_passthrough"


def test_parent_protect_ema_cross_against_parent_does_not_flip_alone() -> None:
    cfg = config_for_variant(VARIANT_EMA_ADX_DI)
    row = _ind_row(
        ema9=90.0,
        ema20=92.0,
        ema59=95.0,
        ema200=100.0,
        atr=1.0,
        adx=30.0,
        plus_di=35.0,
        minus_di=10.0,
        cross_count=0.0,
        expansion=0.8,
        slope9=-0.5,
        slope20=-0.4,
        slope59=-0.2,
        close=89.5,
    )
    new_state, reason = apply_indicator_gate(
        prev_shadow="confirmed_uptrend",
        c31_prev="confirmed_uptrend",
        c31_state="confirmed_downtrend",
        bars_in_shadow=4,
        ind=row,
        cfg=cfg,
        parent_trend="up",
    )
    assert new_state == "confirmed_uptrend"
    assert reason in {"parent_up_protect", "ema_hold_up", "ema_hold_down", "parent_regime_protect"}


def test_high_adx_wrong_di_does_not_confirm_wrong_direction() -> None:
    cfg = config_for_variant(VARIANT_EMA_ADX_DI)
    row = _ind_row(
        ema9=90.0,
        ema20=92.0,
        ema59=95.0,
        ema200=100.0,
        atr=1.0,
        adx=34.0,
        plus_di=38.0,
        minus_di=12.0,
        cross_count=0.0,
        expansion=0.9,
        slope9=-0.5,
        slope20=-0.4,
        slope59=-0.2,
        close=89.0,
    )
    adx_scores = compute_adx_di_scores(row, cfg)
    assert adx_scores["dominant_di"] == "plus"
    new_state, _ = apply_indicator_gate(
        prev_shadow="unclear",
        c31_prev="unclear",
        c31_state="confirmed_downtrend",
        bars_in_shadow=1,
        ind=row,
        cfg=cfg,
        parent_trend=None,
    )
    assert new_state != "confirmed_downtrend"


def test_c32c_can_confirm_without_rising_adx_when_ema_strong() -> None:
    cfg = config_for_variant(VARIANT_EMA)
    row = _ind_row(
        ema9=110.0,
        ema20=108.0,
        ema59=104.0,
        ema200=100.0,
        atr=1.0,
        adx=12.0,
        plus_di=30.0,
        minus_di=15.0,
        cross_count=0.0,
        expansion=0.8,
        slope9=0.6,
        slope20=0.5,
        slope59=0.35,
        slope200=0.1,
        ordered_bull=False,
        close=111.0,
    )
    new_state, _ = apply_indicator_gate(
        prev_shadow="unclear",
        c31_prev="unclear",
        c31_state="confirmed_uptrend",
        bars_in_shadow=1,
        ind=row,
        cfg=cfg,
        parent_trend=None,
    )
    assert new_state == "confirmed_uptrend"


def test_enrich_outcomes_incomplete_horizon_none_not_zero() -> None:
    arrays = {
        "close": np.asarray([100.0, 101.0, 102.0], dtype=float),
        "high": np.asarray([100.5, 101.5, 102.5], dtype=float),
        "low": np.asarray([99.5, 100.5, 101.5], dtype=float),
        "n_bars": 3,
    }
    out = compute_horizon_outcome(
        bar_index=2,
        horizon=5,
        reference_close=102.0,
        side="long",
        arrays=arrays,
    )
    assert out["evaluable"] is False
    assert out["direction_hit"] is None


def test_pine_header_validation() -> None:
    text = "\n".join([*build_pine_header("APTUSDT C3.2 audit"), "// EOF"]) + "\n"
    validate_pine_script(text)


def test_indicator_baseline_matches_c31_with_fake_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_fake_replay(monkeypatch)
    prepared = [_prep_bar(i, f"2026-03-01T00:{i * 5:02d}:00+00:00", 100.0 + i) for i in range(4)]
    arrays = {"n_bars": 4}
    indicators = {
        i: _ind_row(
            ema9=100.0,
            ema20=100.0,
            ema59=100.0,
            ema200=100.0,
            atr=1.0,
            adx=12.0,
            plus_di=20.0,
            minus_di=20.0,
        )
        for i in range(4)
    }
    a0 = pd.Timestamp("2026-03-01T00:00:00+00:00")
    a1 = pd.Timestamp("2026-03-01T00:15:00+00:00")
    baseline = replay_indicator_variant(
        prepared,
        arrays,
        config_c3("conservative"),
        config_for_variant(VARIANT_BASELINE),
        indicators,
        a0,
        a1,
    )
    c31 = replay_regime_variant(
        prepared,
        arrays=arrays,
        cfg=config_c3("conservative"),
        analyze_start=a0,
        analyze_end=a1,
    )
    assert [r["state"] for r in baseline["timeline"]] == [r["state"] for r in c31["timeline"]]


def test_no_lookahead_append_future_bars_does_not_change_past_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fake_replay(monkeypatch)
    short = [_prep_bar(i, f"2026-03-01T00:{i * 5:02d}:00+00:00", 100.0 + i) for i in range(3)]
    long = short + [_prep_bar(3, "2026-03-01T00:15:00+00:00", 103.0)]
    arrays_short = {"n_bars": 3}
    arrays_long = {"n_bars": 4}
    indicators_short = {
        i: _ind_row(
            ema9=100.0,
            ema20=100.0,
            ema59=100.0,
            ema200=100.0,
            atr=1.0,
            adx=12.0,
            plus_di=20.0,
            minus_di=20.0,
        )
        for i in range(3)
    }
    indicators_long = {**indicators_short, 3: indicators_short[2]}
    a0 = pd.Timestamp("2026-03-01T00:00:00+00:00")
    a1_short = pd.Timestamp("2026-03-01T00:10:00+00:00")
    a1_long = pd.Timestamp("2026-03-01T00:15:00+00:00")
    short_replay = replay_indicator_variant(
        short,
        arrays_short,
        config_c3("conservative"),
        config_for_variant(VARIANT_BASELINE),
        indicators_short,
        a0,
        a1_short,
    )
    long_replay = replay_indicator_variant(
        long,
        arrays_long,
        config_c3("conservative"),
        config_for_variant(VARIANT_BASELINE),
        indicators_long,
        a0,
        a1_long,
    )
    assert [r["state"] for r in short_replay["timeline"]] == [r["state"] for r in long_replay["timeline"][:3]]


def test_enrich_event_outcomes_includes_horizon_none_when_incomplete() -> None:
    events = [
        {
            "event_id": "x",
            "direction": "up",
            "attempt_bar_index": 2,
            "attempt_close": 102.0,
            "confirm_bar_index": None,
            "confirm_close": None,
        }
    ]
    arrays = {
        "close": np.asarray([100.0, 101.0, 102.0], dtype=float),
        "high": np.asarray([100.5, 101.5, 102.5], dtype=float),
        "low": np.asarray([99.5, 100.5, 101.5], dtype=float),
        "n_bars": 3,
    }
    enriched = _enrich_event_outcomes(events, arrays=arrays, horizons=(5,))
    assert enriched[0]["attempt_h5_evaluable"] is False
    assert enriched[0]["attempt_h5_direction_hit"] is None
    assert enriched[0]["confirm_h5_evaluable"] is None

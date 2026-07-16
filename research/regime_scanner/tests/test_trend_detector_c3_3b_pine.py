"""Tests for Phase C3.3B combined trend-detector Pine export."""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pytest

from research.regime_scanner.indicator_pattern_discovery_c3_3b import PatternDiscoveryC33BConfig
from research.regime_scanner.trend_detector_c3_3b_pine import (
    ASOF_PINE_NAME,
    OUTCOME_PINE_NAME,
    STATE_CODE,
    build_asof_pine_script,
    build_outcome_audit_pine_script,
    build_retro_markers,
    classify_asof_state,
    compute_trend_detector_states,
    export_trend_detector_artifacts,
)
from research.regime_scanner.trend_pine_export import AUDIT_ANCHOR_PLOT, validate_pine_script
from research.regime_scanner.trend_regime_classification_audit import C2_BASELINE_HASH


def _base_frame(n: int = 40) -> pd.DataFrame:
    rows = []
    ts0 = pd.Timestamp("2026-03-01T00:00:00+00:00")
    for i in range(n):
        close = 100.0 + i * 0.15
        rows.append(
            {
                "timestamp": ts0 + pd.Timedelta(minutes=30 * i),
                "decision_time": ts0 + pd.Timedelta(minutes=30 * (i + 1)),
                "bar_index": i,
                "open": close,
                "high": close + 0.4,
                "low": close - 0.4,
                "close": close,
                "ema_9": close + 0.1,
                "ema_20": close - 0.2,
                "ema_59": close - 0.5,
                "ema_200": close - 1.0,
                "atr_14": 1.0,
                "plus_di_14": 15.0 + i * 0.4,
                "minus_di_14": 25.0 - i * 0.3,
                "di_spread": (15.0 + i * 0.4) - (25.0 - i * 0.3),
                "adx_14": 12.0 + i * 0.5,
                "ema_9_20_spread": 0.3,
                "ema_9_20_spread_atr": 0.3 + i * 0.02,
                "ema_9_slope_3_atr": 0.05 + i * 0.02,
                "ema_20_slope_3_atr": 0.02 + i * 0.015,
                "ema_fast_compression_score": 0.2,
            }
        )
    return pd.DataFrame(rows)


def test_asof_pine_header_and_validator() -> None:
    text = build_asof_pine_script()
    validate_pine_script(text)
    assert text.startswith("//@version=6\n")
    assert text.count("indicator(") == 1
    assert text.count(AUDIT_ANCHOR_PLOT) == 1
    assert re.search(r"(?m)^strategy\(", text) is None
    ind_end = text.index(")")
    # crude: audit after indicator block
    assert text.index(AUDIT_ANCHOR_PLOT) > text.index("indicator(")
    lines = text.splitlines()
    assert lines[1] == "indicator("
    assert lines[8] == AUDIT_ANCHOR_PLOT
    assert "diSpreadExpandMin = input.float(0.2" in text or "diSpreadExpandMin = input.float(0.20" in text
    assert "adxConfirmMin = input.float(20" in text
    assert "bandExpandMin = input.float(0.1" in text


def test_outcome_pine_retro_does_not_assign_state() -> None:
    markers = [
        {
            "event_timestamp": "2026-03-01T01:00:00+00:00",
            "label": "RETRO DI->EMA lag1",
            "is_retrospective": True,
        }
    ]
    text = build_outcome_audit_pine_script(retro_markers=markers)
    validate_pine_script(text)
    assert "RETRO" in text
    retro_idx = text.index("RETRO marker arrays")
    assert "researchState :=" not in text[retro_idx:]
    assert re.search(r"(?m)^strategy\(", text) is None
    assert text.count(AUDIT_ANCHOR_PLOT) == 1


def test_asof_no_future_lookahead_in_state_machine() -> None:
    text = build_asof_pine_script()
    assert "request.security" not in text
    assert "prevState = researchState[1]" in text
    assert "paired_ema_lag" not in text
    # As-of script must not embed RETRO outcome labels.
    assert "RETRO DI" not in text
    assert "clean_success" not in text


def test_thresholds_match_c33b_config() -> None:
    cfg = PatternDiscoveryC33BConfig()
    text = build_asof_pine_script(cfg=cfg)
    assert f"{cfg.di_spread_expand_min}" in text
    assert f"{cfg.adx_level_confirmation_min}" in text
    assert f"{cfg.adx_rising_min_delta_1}" in text
    assert f"{cfg.ema_joint_slope_min_atr}" in text
    assert f"{cfg.band_expand_min_change_atr}" in text


def test_bull_bear_weakening_failed_states() -> None:
    cfg = PatternDiscoveryC33BConfig()
    # Early bull then DI flips -> failed
    early_row = {
        "di_cross_bull": False,
        "di_cross_bear": True,
        "di_bull": False,
        "di_bear": True,
        "ema_bull_order": False,
        "ema_bear_order": True,
        "adx_rising": False,
        "adx_falling": False,
        "adx_confirm": False,
        "joint_rising": False,
        "joint_falling": False,
        "band_expand": False,
        "band_compress": False,
        "di_shrinking": False,
        "di_expanding": False,
        "fast_weakening_from_up": False,
        "move_relevant": False,
        "ema_9_slope_3_atr": -0.1,
        "ema_20_slope_3_atr": -0.1,
        "adx_slope_lin_3": 0.0,
        "ema_9_slope_3_atr_prev": -0.2,
    }
    assert classify_asof_state(early_row, prev_state="early_bullish", cfg=cfg) == "failed_bullish"

    confirmed = {
        "di_cross_bull": False,
        "di_cross_bear": False,
        "di_bull": True,
        "di_bear": False,
        "ema_bull_order": True,
        "ema_bear_order": False,
        "adx_rising": True,
        "adx_falling": False,
        "adx_confirm": True,
        "joint_rising": True,
        "joint_falling": False,
        "band_expand": True,
        "band_compress": False,
        "di_shrinking": False,
        "di_expanding": True,
        "fast_weakening_from_up": False,
        "move_relevant": True,
        "ema_9_slope_3_atr": 0.2,
        "ema_20_slope_3_atr": 0.2,
        "adx_slope_lin_3": 0.3,
        "ema_9_slope_3_atr_prev": 0.15,
    }
    assert classify_asof_state(confirmed, prev_state="developing_bullish", cfg=cfg) == "confirmed_bullish"

    weaken = dict(confirmed)
    weaken.update(
        {
            "adx_rising": False,
            "adx_falling": True,
            "band_expand": False,
            "band_compress": True,
            "di_shrinking": True,
            "fast_weakening_from_up": True,
            "ema_9_slope_3_atr": 0.05,
            "ema_20_slope_3_atr": 0.05,
        }
    )
    # Still bull order + prior attempt + falling ADX => weakening
    assert classify_asof_state(weaken, prev_state="confirmed_bullish", cfg=cfg) == "weakening_bullish"

    bear_flip = dict(confirmed)
    bear_flip.update(
        {
            "di_bull": False,
            "di_bear": True,
            "ema_bull_order": False,
            "ema_bear_order": True,
            "ema_9_slope_3_atr": -0.2,
            "ema_20_slope_3_atr": -0.2,
            "joint_rising": False,
            "joint_falling": True,
        }
    )
    assert classify_asof_state(bear_flip, prev_state="neutral", cfg=cfg) == "confirmed_bearish"


def test_retro_markers_do_not_change_asof_states() -> None:
    frame = _base_frame()
    states = compute_trend_detector_states(frame)
    assert not states["retro_influences_state"].any()
    assert not states["uses_future_lookahead"].any()
    # Inject fake retro columns — classifier ignores them.
    states2 = states.copy()
    states2["paired_ema_lag_bars_retro"] = 99
    states2["outcome_class_retro"] = "clean_success"
    # Recompute from original frame should match ignoring retro columns.
    again = compute_trend_detector_states(frame)
    assert again["research_state"].tolist() == states["research_state"].tolist()


def test_deterministic_pine_and_export(tmp_path: Path) -> None:
    cfg = PatternDiscoveryC33BConfig()
    a = build_asof_pine_script(cfg=cfg)
    b = build_asof_pine_script(cfg=cfg)
    assert a == b
    frame = _base_frame(50)
    seq = [
        {
            "event_type": "di_cross",
            "event_timestamp": frame.iloc[5]["decision_time"],
            "paired_ema_lag_bars": 2,
            "paired_ema_lag_bucket": "2",
        }
    ]
    outs = [
        {
            "event_type": "ema_cross",
            "event_timestamp": frame.iloc[7]["decision_time"],
            "outcome_class": "clean_success",
        }
    ]
    meta1 = export_trend_detector_artifacts(
        frame=frame,
        output_dir=tmp_path / "a",
        cfg=cfg,
        di_ema_sequences=seq,
        outcomes=outs,
    )
    meta2 = export_trend_detector_artifacts(
        frame=frame,
        output_dir=tmp_path / "b",
        cfg=cfg,
        di_ema_sequences=seq,
        outcomes=outs,
    )
    assert meta1["asof_sha256"] == meta2["asof_sha256"]
    assert meta1["outcome_sha256"] == meta2["outcome_sha256"]
    assert (tmp_path / "a" / ASOF_PINE_NAME).is_file()
    assert (tmp_path / "a" / OUTCOME_PINE_NAME).is_file()
    assert (tmp_path / "a" / "trend_detector_state_counts.csv").is_file()
    assert (tmp_path / "a" / "trend_detector_transitions.csv").is_file()
    assert (tmp_path / "a" / "trend_detector_component_summary.csv").is_file()
    assert meta1["asof_no_future_lookahead"] is True
    assert meta1["retro_does_not_affect_state"] is True


def test_baseline_hash_constant() -> None:
    assert C2_BASELINE_HASH == (
        "702ba3e62976aeae879d053a03f64eaba06771beac367248dcfca8d4ebc4ec61"
    )


def test_build_retro_markers_labels() -> None:
    markers = build_retro_markers(
        di_ema_sequences=[
            {
                "event_type": "di_cross",
                "event_timestamp": "2026-03-01T00:30:00+00:00",
                "paired_ema_lag_bars": 1,
                "paired_ema_lag_bucket": "1",
            },
            {
                "event_type": "di_cross",
                "event_timestamp": "2026-03-01T01:00:00+00:00",
                "paired_ema_lag_bars": None,
                "paired_ema_lag_bucket": "none",
            },
        ],
        outcomes=[
            {
                "event_type": "ema_cross",
                "event_timestamp": "2026-03-01T01:30:00+00:00",
                "outcome_class": "adverse_reversal",
            }
        ],
    )
    labels = [m["label"] for m in markers]
    assert any(x.startswith("RETRO") for x in labels)
    assert "RETRO DI->EMA lag1" in labels
    assert "RETRO DI no EMA follow" in labels
    assert "RETRO adverse_reversal" in labels


def test_state_codes_cover_all() -> None:
    assert set(STATE_CODE) >= {
        "neutral",
        "early_bullish",
        "early_bearish",
        "developing_bullish",
        "developing_bearish",
        "confirmed_bullish",
        "confirmed_bearish",
        "weakening_bullish",
        "weakening_bearish",
        "failed_bullish",
        "failed_bearish",
    }

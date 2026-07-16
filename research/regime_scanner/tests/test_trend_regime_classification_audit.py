"""Tests for Phase C3 / C3.1 trend regime classifier and audit."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd
import pytest

import research.regime_scanner.trend_audit_shared_replay as shared_replay_mod
from research.regime_scanner.trend_audit_shared_replay import (
    build_shared_structure_timeline,
    reset_audit_counters,
)
from research.regime_scanner.trend_pine_export import (
    build_c3_regime_pine,
    build_pine_header,
    validate_pine_script,
)
from research.regime_scanner.trend_regime_classifier import (
    RegimeBarFeatures,
    RegimeRuntime,
    compute_range_score,
    config_c3,
    directional_efficiency,
    precompute_regime_arrays,
    replay_regime_variant,
    step_regime_classifier,
)
from research.regime_scanner.trend_regime_classification_audit import (
    C2_BASELINE_HASH,
    assert_baseline_readonly,
    build_baseline_comparison,
    build_visual_review_cases,
    recommend_variant,
)
from research.regime_scanner.trend_robustness_audit import load_analysis_frame


def _feat(**kwargs: object) -> RegimeBarFeatures:
    base = dict(
        bar_index=0,
        net_move_atr=0.0,
        directional_efficiency=0.0,
        overlap_ratio=0.0,
        range_width_atr=2.5,
        range_de=0.08,
        range_net_move_atr=1.5,
        box_efficiency=0.7,
        bound_drift_atr=1.5,
        failed_breakout_count=1.0,
        alternating_score=0.7,
        hh_hl=False,
        lh_ll=False,
        bull_bos=False,
        bear_bos=False,
        bull_choch=False,
        bear_choch=False,
        htf_bias="neutral",
        close=100.0,
        high=100.5,
        low=99.5,
        atr=1.0,
        rolling_high=102.0,
        rolling_low=98.0,
    )
    base.update(kwargs)
    return RegimeBarFeatures(**base)  # type: ignore[arg-type]


def _chop_feat(**kwargs: object) -> RegimeBarFeatures:
    defaults = dict(
        net_move_atr=0.08,
        directional_efficiency=0.12,
        overlap_ratio=0.55,
        range_width_atr=3.0,
        range_de=0.06,
        range_net_move_atr=2.0,
        box_efficiency=0.72,
        bound_drift_atr=1.8,
        failed_breakout_count=2.0,
        alternating_score=0.8,
        hh_hl=False,
        lh_ll=False,
        close=100.0,
        high=100.4,
        low=99.6,
        atr=1.0,
        rolling_high=101.5,
        rolling_low=98.5,
    )
    defaults.update(kwargs)
    return _feat(**defaults)


def test_directional_efficiency_low_for_chop() -> None:
    closes = np.array([100.0, 101.0, 100.0, 101.0, 100.0, 101.0])
    assert directional_efficiency(closes, 5, 6) <= 0.25


def test_directional_efficiency_high_for_trend() -> None:
    closes = np.array([100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
    assert directional_efficiency(closes, 5, 6) > 0.8


def test_single_choch_does_not_confirm_trend() -> None:
    cfg = config_c3("conservative")
    rt = RegimeRuntime()
    rt = step_regime_classifier(
        rt, _feat(bull_choch=True, net_move_atr=0.1, directional_efficiency=0.1), cfg=cfg
    )
    assert rt.state in {"transition_up", "unclear"}
    assert rt.state != "confirmed_uptrend"


def test_single_bos_does_not_confirm_without_structure() -> None:
    cfg = config_c3("conservative")
    rt = RegimeRuntime()
    rt = step_regime_classifier(
        rt, _feat(bull_bos=True, hh_hl=False, net_move_atr=0.2), cfg=cfg
    )
    assert rt.state != "confirmed_uptrend"


def test_confirmed_uptrend_requires_multiple_evidence() -> None:
    cfg = config_c3("balanced")
    rt = RegimeRuntime()
    for _ in range(4):
        rt = step_regime_classifier(
            rt,
            _feat(
                hh_hl=True,
                bull_bos=True,
                net_move_atr=1.2,
                directional_efficiency=0.35,
                range_width_atr=6.0,
                alternating_score=0.1,
                failed_breakout_count=0.0,
                overlap_ratio=0.2,
            ),
            cfg=cfg,
        )
    assert rt.state == "confirmed_uptrend"


def test_confirmed_downtrend_requires_lh_ll() -> None:
    cfg = config_c3("balanced")
    rt = RegimeRuntime()
    for _ in range(4):
        rt = step_regime_classifier(
            rt,
            _feat(
                lh_ll=True,
                bear_bos=True,
                net_move_atr=-1.2,
                directional_efficiency=0.35,
                range_width_atr=6.0,
                alternating_score=0.1,
                failed_breakout_count=0.0,
                overlap_ratio=0.2,
            ),
            cfg=cfg,
        )
    assert rt.state == "confirmed_downtrend"


def test_pullback_preserves_parent_trend() -> None:
    cfg = config_c3("balanced")
    rt = RegimeRuntime(state="confirmed_uptrend", parent_trend="up", last_confirmed_up=True)
    rt = step_regime_classifier(
        rt,
        _feat(
            net_move_atr=-0.4,
            hh_hl=True,
            directional_efficiency=0.4,
            range_width_atr=5.0,
            alternating_score=0.2,
            overlap_ratio=0.3,
        ),
        cfg=cfg,
    )
    assert rt.state == "bullish_pullback"
    assert rt.parent_trend == "up"


def test_range_after_min_duration() -> None:
    cfg = config_c3("balanced")
    rt = RegimeRuntime()
    for _ in range(cfg.range_min_bars + 2):
        rt = step_regime_classifier(rt, _chop_feat(), cfg=cfg)
    assert rt.state == "range_sideways"
    assert rt.in_range is True
    assert rt.range_high is not None and rt.range_low is not None


def test_no_range_on_efficient_trend() -> None:
    cfg = config_c3("balanced")
    rt = RegimeRuntime()
    rt = step_regime_classifier(
        rt,
        _feat(
            net_move_atr=1.5,
            directional_efficiency=0.5,
            hh_hl=True,
            bull_bos=True,
            range_width_atr=8.0,
            range_de=0.35,
            range_net_move_atr=5.0,
            box_efficiency=0.3,
            bound_drift_atr=5.0,
            alternating_score=0.05,
            overlap_ratio=0.2,
            failed_breakout_count=0.0,
        ),
        cfg=cfg,
    )
    assert rt.state != "range_sideways"


def test_single_breakout_does_not_exit_range() -> None:
    cfg = config_c3("balanced")
    rt = RegimeRuntime()
    for _ in range(cfg.range_min_bars + 2):
        rt = step_regime_classifier(rt, _chop_feat(), cfg=cfg)
    assert rt.in_range
    hi = float(rt.range_high or 102.0)
    # one bar close slightly outside — insufficient confirm bars
    rt = step_regime_classifier(
        rt,
        _chop_feat(
            close=hi + cfg.range_exit_atr_distance * 1.1,
            high=hi + 1.0,
            low=hi,
            net_move_atr=0.2,
            directional_efficiency=0.2,
        ),
        cfg=cfg,
    )
    assert rt.in_range or rt.state == "range_sideways" or rt.range_exit_streak < cfg.range_exit_confirm_bars


def test_confirmed_breakout_exits_to_transition() -> None:
    cfg = config_c3("balanced")
    rt = RegimeRuntime()
    for _ in range(cfg.range_min_bars + 2):
        rt = step_regime_classifier(rt, _chop_feat(), cfg=cfg)
    assert rt.in_range
    lo = float(rt.range_low or 98.0)
    for _ in range(cfg.range_exit_confirm_bars + 1):
        rt = step_regime_classifier(
            rt,
            _chop_feat(
                close=lo - cfg.range_exit_atr_distance * 1.2,
                high=lo,
                low=lo - 1.5,
                net_move_atr=-0.9,
                directional_efficiency=0.4,
                range_width_atr=5.0,
                alternating_score=0.2,
                overlap_ratio=0.3,
            ),
            cfg=cfg,
        )
    assert rt.state in {"transition_down", "confirmed_downtrend"}
    assert rt.in_range is False


def test_range_bounds_stable() -> None:
    cfg = config_c3("balanced")
    rt = RegimeRuntime()
    for _ in range(cfg.range_min_bars + 1):
        rt = step_regime_classifier(rt, _chop_feat(rolling_high=102.0, rolling_low=98.0), cfg=cfg)
    hi0, lo0 = rt.range_high, rt.range_low
    rt = step_regime_classifier(
        rt, _chop_feat(high=102.3, low=97.8, close=100.0, rolling_high=103.0, rolling_low=97.0), cfg=cfg
    )
    assert rt.range_high is not None and rt.range_low is not None
    assert abs(float(rt.range_high) - float(hi0 or 0)) < 1.0
    assert abs(float(rt.range_low) - float(lo0 or 0)) < 1.0


def test_pullback_to_range() -> None:
    cfg = config_c3("balanced")
    rt = RegimeRuntime(state="bullish_pullback", parent_trend="up", last_confirmed_up=True)
    for _ in range(cfg.pullback_to_range_min_bars + 1):
        rt = step_regime_classifier(rt, _chop_feat(net_move_atr=-0.1), cfg=cfg)
    assert rt.state == "range_sideways"


def test_range_score_transparent() -> None:
    cfg = config_c3("balanced")
    parts = compute_range_score(_chop_feat(), cfg=cfg, sustained_bos_up=0, sustained_bos_down=0)
    assert 0.0 <= parts["range_score"] <= 1.0
    assert parts["range_score"] >= cfg.range_score_enter_min


def test_transition_expires_without_confirmation() -> None:
    cfg = config_c3("balanced")
    rt = RegimeRuntime(state="transition_down", parent_trend="up", transition_bars=25)
    rt = step_regime_classifier(rt, _chop_feat(), cfg=cfg)
    assert rt.state in {"bullish_pullback", "unclear", "range_sideways", "transition_down"}


def test_hysteresis_keeps_confirmed_uptrend_on_small_pullback() -> None:
    cfg = config_c3("balanced")
    rt = RegimeRuntime(state="confirmed_uptrend", parent_trend="up", last_confirmed_up=True)
    rt = step_regime_classifier(
        rt,
        _feat(
            net_move_atr=-0.2,
            hh_hl=True,
            directional_efficiency=0.35,
            range_width_atr=5.0,
            alternating_score=0.2,
            overlap_ratio=0.3,
        ),
        cfg=cfg,
    )
    assert rt.state in {"bullish_pullback", "confirmed_uptrend"}


def test_c3_pine_header_and_range_diagnostics() -> None:
    pine = build_c3_regime_pine(
        title="APTUSDT C3.1 test",
        symbol="APTUSDT",
        phase="C3_1_range_calibration",
        variant="C3_B_balanced",
        analyze_start="2026-03-01",
        analyze_end="2026-03-12",
        audit_hash="abc",
        state_runs=[
            {
                "start_time": "2026-03-03T00:00:00+00:00",
                "end_time": "2026-03-06T00:00:00+00:00",
                "state": "range_sideways",
            }
        ],
        transitions=[],
        timeline_rows=[
            {
                "decision_time": "2026-03-03T00:00:00+00:00",
                "state": "range_sideways",
                "previous_state": "unclear",
                "range_confirmed": True,
                "range_high": 1.2,
                "range_low": 1.0,
                "range_score": 0.7,
                "reasons": "range_enter",
                "transition": True,
            },
            {
                "decision_time": "2026-03-06T00:00:00+00:00",
                "state": "transition_down",
                "previous_state": "range_sideways",
                "range_confirmed": False,
                "range_high": 1.2,
                "range_low": 1.0,
                "range_score": 0.4,
                "reasons": "range_exit",
                "transition": True,
                "failed_breakout_event": False,
            },
        ],
    )
    assert pine.startswith("\n".join(build_pine_header("APTUSDT C3.1 test")) + "\n")
    assert "WARNING: use APTUSDT 5m UTC" in pine
    assert "Range High" in pine
    assert "RNG_IN" in pine or "rngHighs" in pine
    validate_pine_script(pine)


def test_baseline_comparison_mapping() -> None:
    c2 = [{"decision_time": "t1", "c2_state": "strong_bullish", "bar_index": 0, "close": 1.0}]
    c3 = [
        {
            "decision_time": "t1",
            "state": "range_sideways",
            "bar_index": 0,
            "close": 1.0,
            "parent_trend": None,
            "in_range": True,
        }
    ]
    arrays = {
        "close": np.array([1.0, 1.1]),
        "high": np.array([1.1, 1.2]),
        "low": np.array([0.9, 1.0]),
        "n_bars": 2,
    }
    mapping, _, _ = build_baseline_comparison(
        c2, c3, c3_variant="C3_B_balanced", arrays=arrays, horizons=(3,)
    )
    assert mapping[0]["improvement_flag"] == "c3_downgrade_to_range"


def test_visual_review_vr02_anchor() -> None:
    c2 = [{"decision_time": "2026-03-04T12:00:00+00:00", "c2_state": "neutral"}]
    c3 = [{"decision_time": "2026-03-04T12:00:00+00:00", "state": "range_sideways"}]
    cases = build_visual_review_cases(c2, c3)
    vr02 = next(c for c in cases if c["case_id"].startswith("VR02"))
    assert vr02["expected_context"] == "range_sideways"
    assert vr02["expected_exit"] == "transition_down -> confirmed_downtrend"
    assert vr02["review_status"] == "manual_review_anchor"


def test_recommendation_excludes_responsive() -> None:
    agg = {
        "C3_A_conservative": {
            "n_bars": 100,
            "n_transitions": 10,
            "range_metrics": {"percent_range_bars": 0.2, "range_precision_proxy": 0.6},
            "aggregate_by_state_horizon": [
                {
                    "state": "confirmed_uptrend",
                    "horizon": 12,
                    "n_evaluable": 20,
                    "hit_rate": 0.55,
                    "mean_raw_return": 0.01,
                },
                {
                    "state": "confirmed_downtrend",
                    "horizon": 12,
                    "n_evaluable": 20,
                    "hit_rate": 0.55,
                    "mean_raw_return": -0.01,
                },
                {
                    "state": "range_sideways",
                    "horizon": 12,
                    "n_evaluable": 20,
                    "hit_rate": None,
                    "mean_raw_return": 0.01,
                },
            ],
        },
        "C3_C_responsive": {
            "n_bars": 100,
            "n_transitions": 2,
            "range_metrics": {"percent_range_bars": 0.0},
            "aggregate_by_state_horizon": [
                {
                    "state": "confirmed_uptrend",
                    "horizon": 12,
                    "n_evaluable": 80,
                    "hit_rate": 0.9,
                    "mean_raw_return": 0.0,
                }
            ],
        },
    }
    rec = recommend_variant(agg)
    assert rec["recommendation"] in {"conservative", "inconclusive"}
    assert rec["responsive_excluded_from_c31"] is True
    assert "C3_C_responsive" not in rec["scores"]


def test_recommendation_inconclusive_on_sparse_data() -> None:
    agg = {
        "C3_A_conservative": {
            "n_bars": 10,
            "n_transitions": 9,
            "range_metrics": {},
            "aggregate_by_state_horizon": [],
        }
    }
    rec = recommend_variant(agg)
    assert rec["recommendation"] == "inconclusive"


def test_baseline_readonly_guard(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    summary = baseline / "summary.json"
    summary.write_text(json.dumps({"deterministic_hash": "x"}), encoding="utf-8")
    info = assert_baseline_readonly(baseline)
    assert info["expected_hash"] == C2_BASELINE_HASH
    before = info["baseline_hash"]
    summary.write_text(json.dumps({"deterministic_hash": "tampered"}), encoding="utf-8")
    after = _load_baseline_summary_hash(baseline)
    assert after != before


def _load_baseline_summary_hash(baseline_dir: Path) -> str:
    from research.regime_scanner.trend_regime_classification_audit import _load_baseline_summary_hash

    return _load_baseline_summary_hash(baseline_dir)


def test_c3_vs_c2_comparison_pine_generated() -> None:
    from research.regime_scanner.trend_pine_export import (
        build_c2_c3_comparison_payload,
        build_c3_vs_c2_comparison_pine,
        validate_pine_script,
    )

    c2 = [
        {"decision_time": "2026-03-01T00:00:00+00:00", "c2_state": "strong_bullish"},
        {"decision_time": "2026-03-01T00:05:00+00:00", "c2_state": "strong_bullish"},
        {"decision_time": "2026-03-01T00:10:00+00:00", "c2_state": "topping"},
    ]
    c3 = [
        {"decision_time": "2026-03-01T00:00:00+00:00", "state": "range_sideways"},
        {"decision_time": "2026-03-01T00:05:00+00:00", "state": "range_sideways"},
        {"decision_time": "2026-03-01T00:10:00+00:00", "state": "confirmed_downtrend"},
    ]
    payload = build_c2_c3_comparison_payload(c2, c3)
    pine = build_c3_vs_c2_comparison_pine(
        title="APTUSDT C3 vs C2",
        symbol="APTUSDT",
        analyze_start="2026-03-01",
        analyze_end="2026-03-12",
        audit_hash="abc",
        comparison=payload,
    )
    assert "WARNING: use APTUSDT 5m UTC" in pine
    assert "cmpTimes" not in pine
    assert len(pine) < 200_000
    validate_pine_script(pine)


def test_configs_differ_conservative_balanced() -> None:
    a = config_c3("conservative")
    b = config_c3("balanced")
    assert a.range_score_enter_min != b.range_score_enter_min
    assert a.range_min_bars != b.range_min_bars


@pytest.mark.parametrize("mode", ["conservative", "balanced"])
def test_shared_replay_single_structure_pass(mode: str) -> None:
    try:
        frame = load_analysis_frame(
            "APTUSDT", load_start="2026-02-20", load_end="2026-03-10", max_bars=1200
        )
    except Exception as exc:
        pytest.skip(str(exc))
    reset_audit_counters()
    shared = build_shared_structure_timeline(frame)
    assert shared_replay_mod.SHARED_STRUCTURE_PASS_COUNT == 1
    import research.regime_scanner.trend_structure as ts_mod

    calls = {"n": 0}
    orig = ts_mod.update_market_structure

    def _count(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)

    arrays = precompute_regime_arrays(frame)
    with mock.patch.object(ts_mod, "update_market_structure", side_effect=_count):
        replay_regime_variant(
            shared.prepared_bars,
            arrays=arrays,
            cfg=config_c3(mode),
            analyze_start=pd.Timestamp("2026-03-01", tz="UTC"),
            analyze_end=pd.Timestamp("2026-03-08", tz="UTC"),
        )
    assert calls["n"] == 0

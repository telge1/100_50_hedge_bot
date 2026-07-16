"""Tests for Phase C3.3A indicator pattern discovery."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from research.regime_scanner.indicator_pattern_discovery import (
    PatternDiscoveryConfig,
    _pine_dmi_script,
    _pine_event_script,
    aggregate_pattern_metrics,
    assign_pattern_families,
    build_candidate_patterns,
    compute_event_outcomes,
    compute_timing_features,
    detect_adx_dynamics,
    detect_di_crosses,
    detect_ema_crosses,
    detect_ema_expansions,
    detect_range_breakouts,
    detect_trend_follow,
    events_content_hash,
    extract_event_windows,
    indicator_quantiles,
    run_audit,
    sensitivity_check,
    split_discovery_validation,
)
from research.regime_scanner.trend_pine_export import validate_pine_script


def _frame(rows: list[dict[str, object]], *, symbol: str = "APTUSDT") -> pd.DataFrame:
    base_ts = pd.Timestamp("2026-03-01T00:00:00+00:00")
    out: list[dict[str, object]] = []
    for i, spec in enumerate(rows):
        ts = base_ts + pd.Timedelta(minutes=30 * i)
        row = {
            "symbol": symbol,
            "timeframe": "30m",
            "timestamp": ts,
            "decision_time": ts + pd.Timedelta(minutes=30),
            "bar_index": i,
            "open": float(spec.get("open", spec.get("close", 100.0))),
            "high": float(spec.get("high", spec.get("close", 100.0) + 0.5)),
            "low": float(spec.get("low", spec.get("close", 100.0) - 0.5)),
            "close": float(spec.get("close", 100.0)),
            "volume": float(spec.get("volume", 1000.0)),
            "features_ready": bool(spec.get("features_ready", True)),
            "ema_9": float(spec.get("ema_9", spec.get("close", 100.0))),
            "ema_20": float(spec.get("ema_20", spec.get("close", 100.0) + 0.5)),
            "ema_59": float(spec.get("ema_59", spec.get("close", 100.0) + 1.0)),
            "ema_200": float(spec.get("ema_200", spec.get("close", 100.0) + 2.0)),
            "atr_14": float(spec.get("atr_14", 1.0)),
            "ema_9_20_spread": float(spec.get("ema_9_20_spread", -0.5)),
            "ema_9_20_spread_atr": float(spec.get("ema_9_20_spread_atr", -0.5)),
            "ema_9_20_abs_spread_atr": float(abs(spec.get("ema_9_20_spread_atr", -0.5))),
            "ema_9_20_spread_change_3_atr": float(spec.get("ema_9_20_spread_change_3_atr", 0.0)),
            "ema_9_slope_3_atr": float(spec.get("ema_9_slope_3_atr", 0.0)),
            "ema_20_slope_3_atr": float(spec.get("ema_20_slope_3_atr", 0.0)),
            "ema_59_slope_3_atr": float(spec.get("ema_59_slope_3_atr", 0.0)),
            "ema_200_slope_3_atr": float(spec.get("ema_200_slope_3_atr", 0.0)),
            "ema_fast_compression_score": float(spec.get("ema_fast_compression_score", 0.2)),
            "ema_fast_expansion_score": float(spec.get("ema_fast_expansion_score", 0.2)),
            "ema_bullish_ordered": bool(spec.get("ema_bullish_ordered", False)),
            "ema_bearish_ordered": bool(spec.get("ema_bearish_ordered", False)),
            "plus_di_14": float(spec.get("plus_di_14", 20.0)),
            "minus_di_14": float(spec.get("minus_di_14", 20.0)),
            "di_spread": float(spec.get("di_spread", 0.0)),
            "adx_14": float(spec.get("adx_14", 15.0)),
            "adx_slope_3": float(spec.get("adx_slope_3", 0.0)),
            "adx_slope_6": float(spec.get("adx_slope_6", 0.0)),
            "close_to_ema_20_atr": float(spec.get("close_to_ema_20_atr", 0.0)),
            "close_to_ema_59_atr": float(spec.get("close_to_ema_59_atr", 0.0)),
            "close_to_ema_200_atr": float(spec.get("close_to_ema_200_atr", 0.0)),
            "regime_proxy": str(spec.get("regime_proxy", "range")),
            "regime_proxy_direction": str(spec.get("regime_proxy_direction", "range")),
            "range_high": float(spec.get("range_high", 100.0)),
            "range_low": float(spec.get("range_low", 98.0)),
            "range_mid": float(spec.get("range_mid", 99.0)),
            "range_width_atr": float(spec.get("range_width_atr", 2.0)),
            "range_breakout_upper": float(spec.get("range_breakout_upper", 100.15)),
            "range_breakout_lower": float(spec.get("range_breakout_lower", 97.85)),
        }
        out.append(row)
    return pd.DataFrame(out)


def _simple_event_sig(events: list[dict[str, object]]) -> list[tuple[str, int, str]]:
    return [
        (str(ev["event_type"]), int(ev["bar_index"]), str(ev["event_timestamp"]))
        for ev in events
    ]


def test_ema_cross_emits_once() -> None:
    frame = _frame(
        [
            {"ema_9_20_spread": -1.0, "ema_9_20_spread_atr": -1.0, "close": 99.0},
            {"ema_9_20_spread": -0.5, "ema_9_20_spread_atr": -0.5, "close": 99.2},
            {"ema_9_20_spread": 0.3, "ema_9_20_spread_atr": 0.3, "close": 100.5, "ema_bullish_ordered": True},
            {"ema_9_20_spread": 0.6, "ema_9_20_spread_atr": 0.6, "close": 101.0, "ema_bullish_ordered": True},
        ]
    )
    events = detect_ema_crosses(frame)
    assert len(events) == 1
    assert events[0]["direction"] == "bullish"
    assert events[0]["cross_in_range"] is True


def test_ema_expansion_has_single_start() -> None:
    frame = _frame(
        [
            {
                "ema_9_20_spread_atr": 0.1,
                "ema_9_20_spread_change_3_atr": 0.0,
                "ema_9_slope_3_atr": 0.01,
                "ema_20_slope_3_atr": 0.01,
                "ema_fast_expansion_score": 0.0,
                "ema_bullish_ordered": True,
            },
            {
                "ema_9_20_spread_atr": 0.2,
                "ema_9_20_spread_change_3_atr": 0.09,
                "ema_9_slope_3_atr": 0.03,
                "ema_20_slope_3_atr": 0.02,
                "ema_fast_expansion_score": 0.1,
                "ema_bullish_ordered": True,
            },
            {
                "ema_9_20_spread_atr": 0.3,
                "ema_9_20_spread_change_3_atr": 0.11,
                "ema_9_slope_3_atr": 0.04,
                "ema_20_slope_3_atr": 0.03,
                "ema_fast_expansion_score": 0.2,
                "ema_bullish_ordered": True,
            },
            {
                "ema_9_20_spread_atr": 0.4,
                "ema_9_20_spread_change_3_atr": 0.12,
                "ema_9_slope_3_atr": 0.05,
                "ema_20_slope_3_atr": 0.04,
                "ema_fast_expansion_score": 0.3,
                "ema_bullish_ordered": True,
            },
        ]
    )
    events = detect_ema_expansions(frame)
    assert len(events) == 1
    assert events[0]["aligned_slopes"] is True


def test_di_cross_emits_once() -> None:
    frame = _frame(
        [
            {"di_spread": -2.0},
            {"di_spread": -1.0},
            {"di_spread": 1.5},
            {"di_spread": 2.0},
        ]
    )
    events = detect_di_crosses(frame)
    assert len(events) == 1
    assert events[0]["direction"] == "bullish"


def test_wick_pierce_without_close_outside_has_no_breakout() -> None:
    frame = _frame(
        [
            {"close": 99.0, "high": 100.0, "low": 98.5, "range_breakout_upper": 100.15, "range_breakout_lower": 97.85},
            {"close": 99.5, "high": 101.0, "low": 97.9, "range_breakout_upper": 100.15, "range_breakout_lower": 97.85},
        ]
    )
    assert detect_range_breakouts(frame, PatternDiscoveryConfig(breakout_acceptance_bars=2)) == []


def test_multi_bar_breakout_has_one_lifecycle() -> None:
    frame = _frame(
        [
            {"close": 99.0, "high": 99.6, "low": 98.5, "range_breakout_upper": 100.15, "range_breakout_lower": 97.85},
            {"close": 100.4, "high": 100.6, "low": 100.1, "range_breakout_upper": 100.15, "range_breakout_lower": 97.85},
            {"close": 100.6, "high": 100.8, "low": 100.3, "range_breakout_upper": 100.15, "range_breakout_lower": 97.85},
            {"close": 100.8, "high": 101.0, "low": 100.5, "range_breakout_upper": 100.15, "range_breakout_lower": 97.85},
        ]
    )
    events = detect_range_breakouts(frame, PatternDiscoveryConfig(breakout_acceptance_bars=2))
    assert [ev["event_type"] for ev in events] == ["range_breakout_attempt", "range_breakout_confirmed"]


def test_timing_buckets_di_before_after_ema() -> None:
    frame = _frame([{} for _ in range(10)])
    events = [
        {
            "event_id": "x1",
            "event_type": "ema_cross",
            "direction": "bullish",
            "event_timestamp": "2026-03-01T02:30:00+00:00",
            "bar_index": 5,
            "symbol": "APTUSDT",
            "timeframe": "30m",
            "is_retrospective": False,
        },
        {
            "event_id": "x2",
            "event_type": "di_cross",
            "direction": "bullish",
            "event_timestamp": "2026-03-01T01:30:00+00:00",
            "bar_index": 3,
            "symbol": "APTUSDT",
            "timeframe": "30m",
            "is_retrospective": False,
        },
        {
            "event_id": "x3",
            "event_type": "di_cross",
            "direction": "bullish",
            "event_timestamp": "2026-03-01T03:30:00+00:00",
            "bar_index": 7,
            "symbol": "APTUSDT",
            "timeframe": "30m",
            "is_retrospective": False,
        },
    ]
    timed = compute_timing_features(events, frame, None)
    di_before = next(ev for ev in timed if ev["bar_index"] == 3)
    di_after = next(ev for ev in timed if ev["bar_index"] == 7)
    assert di_before["timing_bucket_di_vs_ema"] == "lead"
    assert di_after["timing_bucket_di_vs_ema"] == "lag_1_3"
    assert "bars_to_next_ema_cross_retro" in di_before


def test_adx_retrospective_marker_and_flags_present() -> None:
    frame = _frame(
        [
            {"adx_14": 10.0, "adx_slope_3": -1.0, "adx_slope_6": -1.0},
            {"adx_14": 9.0, "adx_slope_3": -1.0, "adx_slope_6": -1.0},
            {"adx_14": 8.0, "adx_slope_3": -0.5, "adx_slope_6": -0.5},
            {"adx_14": 9.0, "adx_slope_3": 0.6, "adx_slope_6": 0.6},
            {"adx_14": 10.0, "adx_slope_3": 0.7, "adx_slope_6": 0.7},
            {"adx_14": 11.0, "adx_slope_3": 0.8, "adx_slope_6": 0.8},
            {"adx_14": 10.0, "adx_slope_3": -0.5, "adx_slope_6": -0.5},
        ]
    )
    events = detect_adx_dynamics(frame, PatternDiscoveryConfig())
    assert any(ev["event_type"] == "adx_local_low_retro" and ev["is_retrospective"] for ev in events)
    timed = compute_timing_features(events, frame, None)
    assert any("bars_to_next_ema_cross_retro" in ev for ev in timed)


def test_prefix_replay_keeps_past_events_stable() -> None:
    frame = _frame(
        [
            {"ema_9_20_spread": -1.0, "ema_9_20_spread_atr": -1.0, "di_spread": -2.0, "adx_14": 10.0, "adx_slope_3": -1.0, "adx_slope_6": -1.0},
            {"ema_9_20_spread": -0.8, "ema_9_20_spread_atr": -0.8, "di_spread": -1.2, "adx_14": 9.0, "adx_slope_3": -0.8, "adx_slope_6": -0.8},
            {"ema_9_20_spread": 0.2, "ema_9_20_spread_atr": 0.2, "ema_bullish_ordered": True, "di_spread": -0.2, "adx_14": 8.5, "adx_slope_3": -0.2, "adx_slope_6": -0.2},
            {"ema_9_20_spread": 0.4, "ema_9_20_spread_atr": 0.4, "ema_bullish_ordered": True, "di_spread": 0.3, "adx_14": 9.5, "adx_slope_3": 0.6, "adx_slope_6": 0.6},
            {"ema_9_20_spread": 0.6, "ema_9_20_spread_atr": 0.6, "ema_bullish_ordered": True, "di_spread": 0.8, "adx_14": 10.8, "adx_slope_3": 0.7, "adx_slope_6": 0.7},
            {"ema_9_20_spread": 0.8, "ema_9_20_spread_atr": 0.8, "ema_bullish_ordered": True, "di_spread": 1.0, "adx_14": 11.5, "adx_slope_3": 0.8, "adx_slope_6": 0.8},
        ]
    )
    prefix = frame.iloc[:4].reset_index(drop=True)
    full_events = assign_pattern_families(
        [
            *detect_ema_crosses(frame),
            *detect_di_crosses(frame),
            *detect_adx_dynamics(frame, PatternDiscoveryConfig()),
        ]
    )
    prefix_events = assign_pattern_families(
        [
            *detect_ema_crosses(prefix),
            *detect_di_crosses(prefix),
            *detect_adx_dynamics(prefix, PatternDiscoveryConfig()),
        ]
    )
    full_sig = _simple_event_sig(
        [ev for ev in full_events if int(ev["bar_index"]) < 4 and not bool(ev.get("is_retrospective"))]
    )
    prefix_sig = _simple_event_sig([ev for ev in prefix_events if not bool(ev.get("is_retrospective"))])
    assert full_sig == prefix_sig


def test_outcomes_have_signs_and_incomplete_horizon_none() -> None:
    frame = _frame([{ "close": 100.0 + i } for i in range(10)])
    events = [
        {
            "event_id": "e1",
            "event_type": "ema_cross",
            "direction": "bullish",
            "event_timestamp": frame.iloc[0]["decision_time"].isoformat(),
            "bar_index": 0,
            "symbol": "APTUSDT",
            "timeframe": "30m",
            "is_retrospective": False,
            "close": 100.0,
        },
        {
            "event_id": "e2",
            "event_type": "ema_cross",
            "direction": "bullish",
            "event_timestamp": frame.iloc[-1]["decision_time"].isoformat(),
            "bar_index": 9,
            "symbol": "APTUSDT",
            "timeframe": "30m",
            "is_retrospective": False,
            "close": 109.0,
        },
    ]
    cfg = PatternDiscoveryConfig(delayed_horizon=3)
    enriched = compute_event_outcomes(events, frame, (3, 6), cfg)
    assert enriched[0]["h3_direction_hit"] is True
    assert enriched[0]["outcome_class"] in {"clean_success", "delayed_success"}
    assert enriched[1]["outcome_class"] == "insufficient_horizon"


def test_split_and_candidates_only_from_discovery() -> None:
    events = [
        {
            "event_id": "a",
            "event_type": "ema_cross",
            "pattern_id": "p1",
            "pattern_family": "ema_cross_basic",
            "event_timestamp": "2026-03-01T01:00:00+00:00",
            "bar_index": 1,
            "symbol": "APTUSDT",
            "timeframe": "30m",
            "outcome_class": "clean_success",
            "h24_mfe_pct": 1.0,
            "h24_mae_pct": 0.1,
            "h24_directional_close_return_pct": 1.2,
            "split": "discovery",
        },
        {
            "event_id": "b",
            "event_type": "di_cross",
            "pattern_id": "p1",
            "pattern_family": "ema_cross_basic",
            "event_timestamp": "2026-03-08T01:00:00+00:00",
            "bar_index": 8,
            "symbol": "APTUSDT",
            "timeframe": "30m",
            "outcome_class": "failed_no_followthrough",
            "h24_mfe_pct": 0.2,
            "h24_mae_pct": 0.9,
            "h24_directional_close_return_pct": -0.3,
            "split": "validation",
        },
    ]
    split = split_discovery_validation(events, "2026-03-07T00:00:00+00:00")
    assert len(split["discovery"]) == 1
    assert len(split["validation"]) == 1
    metrics = aggregate_pattern_metrics(events, 1)
    candidates = build_candidate_patterns(metrics["discovery"], metrics["validation"], min_n=1)
    assert {c["pattern_id"] for c in candidates} == {"p1"}
    assert candidates[0]["status"] in {"research_candidate", "rejected_unstable"}


def test_pine_scripts_validate() -> None:
    markers = [
        {"event_timestamp": "2026-03-01T00:30:00+00:00", "label": "candidate", "kind": "candidate"},
        {"event_timestamp": "2026-03-01T01:00:00+00:00", "label": "success", "kind": "success"},
        {"event_timestamp": "2026-03-01T01:30:00+00:00", "label": "failure", "kind": "failure"},
    ]
    overlay = _pine_event_script(
        title="APTUSDT C3.3A overlay",
        symbol="APTUSDT",
        timeframe="30m",
        analyze_start="2026-03-01",
        analyze_end="2026-03-12",
        markers=markers,
    )
    dmi = _pine_dmi_script(
        title="APTUSDT C3.3A DMI",
        symbol="APTUSDT",
        timeframe="30m",
        analyze_start="2026-03-01",
        analyze_end="2026-03-12",
        markers=markers,
    )
    validate_pine_script(overlay)
    validate_pine_script(dmi)
    assert "ta.dmi(14, 14)" in dmi


def test_retrospective_adx_fields_and_hashes() -> None:
    frame = _frame(
        [
            {"adx_14": 10.0, "adx_slope_3": -1.0, "adx_slope_6": -1.0},
            {"adx_14": 9.0, "adx_slope_3": -1.0, "adx_slope_6": -1.0},
            {"adx_14": 8.0, "adx_slope_3": -0.5, "adx_slope_6": -0.5},
            {"adx_14": 9.0, "adx_slope_3": 0.6, "adx_slope_6": 0.6},
            {"adx_14": 11.0, "adx_slope_3": 0.7, "adx_slope_6": 0.7},
        ]
    )
    events = detect_adx_dynamics(frame, PatternDiscoveryConfig())
    assert any(ev["is_retrospective"] for ev in events if ev["event_type"] == "adx_local_low_retro")
    assert events_content_hash(events) == events_content_hash(events)
    q = indicator_quantiles(frame)
    assert "adx_14" in q


def test_sensitivity_reports_stable_counts() -> None:
    frame = _frame(
        [
            {"ema_9_20_spread_change_3_atr": 0.2, "ema_9_20_spread_atr": 0.2, "ema_9_slope_3_atr": 0.1, "ema_20_slope_3_atr": 0.1, "ema_bullish_ordered": True, "ema_fast_expansion_score": 0.5, "close": 100.0},
            {"ema_9_20_spread_change_3_atr": 0.2, "ema_9_20_spread_atr": 0.3, "ema_9_slope_3_atr": 0.1, "ema_20_slope_3_atr": 0.1, "ema_bullish_ordered": True, "ema_fast_expansion_score": 0.5, "close": 101.0},
        ]
    )
    events = compute_event_outcomes(
        assign_pattern_families(detect_ema_expansions(frame)),
        frame,
        (24,),
        PatternDiscoveryConfig(),
    )
    rows = sensitivity_check(events, PatternDiscoveryConfig(), scales=(0.9, 1.1))
    assert len(rows) == 2


def test_end_to_end_audit_runs_on_synthetic(tmp_path: Path, monkeypatch: object) -> None:
    # Smoke the orchestrator on a tiny synthetic frame by mocking the loader chain.
    import research.regime_scanner.indicator_pattern_discovery as mod

    tiny = _frame(
        [
            {"ema_9_20_spread": -1.0, "ema_9_20_spread_atr": -1.0, "di_spread": -1.0, "adx_14": 8.0, "adx_slope_3": -1.0, "adx_slope_6": -1.0},
            {"ema_9_20_spread": 0.2, "ema_9_20_spread_atr": 0.2, "ema_bullish_ordered": True, "di_spread": 0.5, "adx_14": 9.0, "adx_slope_3": 0.7, "adx_slope_6": 0.7},
            {"ema_9_20_spread": 0.4, "ema_9_20_spread_atr": 0.4, "ema_bullish_ordered": True, "di_spread": 0.7, "adx_14": 10.0, "adx_slope_3": 0.8, "adx_slope_6": 0.8},
            {"ema_9_20_spread": 0.6, "ema_9_20_spread_atr": 0.6, "ema_bullish_ordered": True, "di_spread": 0.9, "adx_14": 11.0, "adx_slope_3": 0.9, "adx_slope_6": 0.9},
        ]
    )

    monkeypatch.setattr(mod, "build_discovery_frame", lambda *args, **kwargs: tiny.copy())
    monkeypatch.setattr(
        mod,
        "assert_baseline_readonly",
        lambda baseline_dir: {
            "baseline_dir": str(baseline_dir),
            "baseline_hash": mod.C2_BASELINE_HASH,
            "expected_hash": mod.C2_BASELINE_HASH,
            "hash_matches": True,
            "sha256sums_present": False,
        },
    )
    summary = run_audit(
        output_dir=tmp_path,
        baseline_dir=tmp_path / "baseline",
        discovery_end="2026-03-07T00:00:00+00:00",
        min_pattern_events=1,
    )
    assert summary["deterministic_hash"]

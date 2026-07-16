"""Tests for Phase C3.3B indicator pattern discovery."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

import research.regime_scanner.indicator_pattern_discovery_c3_3b as mod
from research.regime_scanner.indicator_pattern_discovery_c3_3b import (
    PatternDiscoveryC33BConfig,
    _assign_pattern_ids,
    _policy_flags,
    build_adx_asof_relationships,
    build_candidate_patterns_c33b,
    build_di_ema_sequences,
    build_ema_band_dynamics,
    compute_multi_horizon_outcomes_c33b,
    enrich_discovery_frame,
    pattern_component_ablation,
    run_c33b_audit,
    threshold_sensitivity_c33b,
)
from research.regime_scanner.trend_regime_classification_audit import C2_BASELINE_HASH


def _frame(rows: list[dict[str, object]], *, symbol: str = "APTUSDT") -> pd.DataFrame:
    base_ts = pd.Timestamp("2026-03-01T00:00:00+00:00")
    out: list[dict[str, object]] = []
    for i, spec in enumerate(rows):
        ts = base_ts + pd.Timedelta(minutes=30 * i)
        close = float(spec.get("close", 100.0))
        atr = float(spec.get("atr_14", 1.0))
        row = {
            "symbol": symbol,
            "timeframe": "30m",
            "timestamp": ts,
            "decision_time": ts + pd.Timedelta(minutes=30),
            "bar_index": i,
            "open": float(spec.get("open", close)),
            "high": float(spec.get("high", close + 0.5)),
            "low": float(spec.get("low", close - 0.5)),
            "close": close,
            "volume": float(spec.get("volume", 1000.0)),
            "features_ready": bool(spec.get("features_ready", True)),
            "ema_9": float(spec.get("ema_9", close)),
            "ema_20": float(spec.get("ema_20", close + 0.2)),
            "ema_59": float(spec.get("ema_59", close + 0.8)),
            "ema_200": float(spec.get("ema_200", close + 1.6)),
            "atr_14": atr,
            "ema_9_20_spread": float(spec.get("ema_9_20_spread", -0.2)),
            "ema_9_20_spread_atr": float(spec.get("ema_9_20_spread_atr", -0.2)),
            "ema_9_20_abs_spread_atr": float(abs(spec.get("ema_9_20_spread_atr", -0.2))),
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
            "adx_14": float(spec.get("adx_14", 18.0)),
            "adx_slope_3": float(spec.get("adx_slope_3", 0.0)),
            "adx_slope_6": float(spec.get("adx_slope_6", 0.0)),
            "close_to_ema_20_atr": float(spec.get("close_to_ema_20_atr", 0.0)),
            "close_to_ema_59_atr": float(spec.get("close_to_ema_59_atr", 0.1)),
            "close_to_ema_200_atr": float(spec.get("close_to_ema_200_atr", 0.2)),
            "regime_proxy": str(spec.get("regime_proxy", "range")),
            "regime_proxy_direction": str(spec.get("regime_proxy_direction", "range")),
            "range_high": float(spec.get("range_high", close + 1.0)),
            "range_low": float(spec.get("range_low", close - 1.0)),
            "range_mid": float(spec.get("range_mid", close)),
            "range_width_atr": float(spec.get("range_width_atr", 2.0)),
            "range_breakout_upper": float(spec.get("range_breakout_upper", close + 0.8)),
            "range_breakout_lower": float(spec.get("range_breakout_lower", close - 0.8)),
        }
        out.append(row)
    return pd.DataFrame(out)


def _event(
    *,
    bar_index: int,
    event_type: str,
    direction: str,
    ts: str,
    close: float,
    symbol: str = "APTUSDT",
    timeframe: str = "30m",
    event_id: str | None = None,
) -> dict[str, object]:
    return {
        "event_id": event_id or f"{event_type}:{bar_index}",
        "event_type": event_type,
        "direction": direction,
        "event_timestamp": ts,
        "bar_index": bar_index,
        "symbol": symbol,
        "timeframe": timeframe,
        "close": close,
        "high": close + 0.5,
        "low": close - 0.5,
        "atr_14": 1.0,
    }


def test_config_to_dict_documents_thresholds() -> None:
    cfg = PatternDiscoveryC33BConfig()
    d = cfg.to_dict()
    assert d["horizons"] == (3, 6, 12, 24)
    assert d["optional_horizons"] == (48,)
    assert d["adx_level_confirmation_min"] == 20.0
    assert d["band_expand_min_change_atr"] == 0.10


def test_enrich_frame_adds_requested_columns() -> None:
    frame = _frame(
        [
            {"adx_14": 10.0, "ema_9_slope_3_atr": 0.0, "ema_20_slope_3_atr": 0.0, "ema_9_20_spread_atr": 0.1},
            {"adx_14": 11.0, "ema_9_slope_3_atr": 0.2, "ema_20_slope_3_atr": 0.1, "ema_9_20_spread_atr": 0.2},
            {"adx_14": 13.0, "ema_9_slope_3_atr": 0.3, "ema_20_slope_3_atr": 0.2, "ema_9_20_spread_atr": 0.35},
            {"adx_14": 16.0, "ema_9_slope_3_atr": 0.4, "ema_20_slope_3_atr": 0.3, "ema_9_20_spread_atr": 0.55},
            {"adx_14": 18.0, "ema_9_slope_3_atr": 0.5, "ema_20_slope_3_atr": 0.4, "ema_9_20_spread_atr": 0.80},
        ]
    )
    enriched = enrich_discovery_frame(frame, PatternDiscoveryC33BConfig())
    for col in (
        "adx_delta_1",
        "adx_delta_2",
        "adx_delta_3",
        "adx_delta_5",
        "adx_slope_lin_3",
        "adx_accel",
        "adx_rising_streak",
        "band_change_1_atr",
        "band_change_2_atr",
        "band_change_3_atr",
        "band_change_5_atr",
        "ema_joint_slope_3_atr",
        "ema_band_expansion_duration",
        "ema_band_expansion_ge_2",
        "ema_band_expansion_ge_3",
        "ema_band_expansion_ge_5",
    ):
        assert col in enriched.columns
    assert enriched["adx_rising_streak"].iloc[-1] >= 3


def test_di_leads_ema_exact_lags() -> None:
    cfg = PatternDiscoveryC33BConfig()
    # DI at 0, EMA follow at lags 1,2,3,5,8 across separate directions to avoid collisions.
    specs = [{"di_spread": 0.0, "adx_14": 18.0 + i * 0.2} for i in range(20)]
    frame = enrich_discovery_frame(_frame(specs), cfg)
    cases = [
        (0, 1, "bullish", "1"),
        (2, 4, "bearish", "2"),
        (5, 8, "bullish", "3"),
        (9, 13, "bearish", "4_6"),
        (10, 18, "neutral", "7_12"),
    ]
    events: list[dict[str, object]] = []
    for di_i, ema_i, direction, _bucket in cases:
        events.append(
            _event(
                bar_index=di_i,
                event_type="di_cross",
                direction=direction,
                ts=frame.iloc[di_i]["decision_time"].isoformat(),
                close=100.0,
                event_id=f"di:{di_i}",
            )
        )
        events.append(
            _event(
                bar_index=ema_i,
                event_type="ema_cross",
                direction=direction,
                ts=frame.iloc[ema_i]["decision_time"].isoformat(),
                close=100.0,
                event_id=f"ema:{ema_i}",
            )
        )
    seq = build_di_ema_sequences(events, frame, cfg)
    by_id = {row["event_id"]: row for row in seq}
    for di_i, ema_i, _direction, bucket in cases:
        di_row = by_id[f"di:{di_i}"]
        ema_row = by_id[f"ema:{ema_i}"]
        assert di_row["paired_ema_lag_bars"] == ema_i - di_i
        assert di_row["paired_ema_lag_bucket"] == bucket
        assert di_row["sequence_core"] == "di_leads_ema"
        assert di_row["is_retrospective"] is True
        assert di_row["is_policy_feature"] is False
        assert ema_row["sequence_core"] == "di_with_ema_follow"
        assert ema_row["has_prior_di_cross_asof"] is True
        assert ema_row["is_policy_feature"] is True


def test_di_ema_coincident_and_without_follow() -> None:
    cfg = PatternDiscoveryC33BConfig()
    frame = enrich_discovery_frame(
        _frame([{"di_spread": float(i), "adx_14": 20.0} for i in range(8)]),
        cfg,
    )
    events = [
        _event(bar_index=1, event_type="di_cross", direction="bullish", ts=frame.iloc[1]["decision_time"].isoformat(), close=100.0, event_id="di_c"),
        _event(bar_index=1, event_type="ema_cross", direction="bullish", ts=frame.iloc[1]["decision_time"].isoformat(), close=100.0, event_id="ema_c"),
        _event(bar_index=3, event_type="di_cross", direction="bearish", ts=frame.iloc[3]["decision_time"].isoformat(), close=99.0, event_id="di_solo"),
        _event(bar_index=5, event_type="ema_cross", direction="neutral", ts=frame.iloc[5]["decision_time"].isoformat(), close=101.0, event_id="ema_solo"),
    ]
    seq = build_di_ema_sequences(events, frame, cfg)
    by_id = {row["event_id"]: row for row in seq}
    assert by_id["di_c"]["sequence_core"] == "di_ema_coincident"
    assert by_id["di_c"]["paired_ema_lag_bars"] == 0
    assert by_id["di_c"]["is_policy_feature"] is True
    assert by_id["ema_c"]["sequence_core"] == "di_ema_coincident"
    assert by_id["di_solo"]["sequence_core"] == "di_without_ema_follow"
    assert by_id["di_solo"]["is_retrospective"] is True
    assert by_id["ema_solo"]["sequence_core"] == "ema_without_prior_di"
    assert by_id["ema_solo"]["is_policy_feature"] is True
    assert "di_spread_expanding_after_1_path" in by_id["di_c"]


def test_adx_rising_and_falling_during_expansion() -> None:
    cfg = PatternDiscoveryC33BConfig()
    rising = enrich_discovery_frame(
        _frame(
            [
                {"adx_14": 18.0, "ema_9_20_spread_atr": 0.10, "ema_9_slope_3_atr": 0.2, "ema_20_slope_3_atr": 0.2},
                {"adx_14": 18.5, "ema_9_20_spread_atr": 0.25, "ema_9_slope_3_atr": 0.25, "ema_20_slope_3_atr": 0.25},
                {"adx_14": 19.2, "ema_9_20_spread_atr": 0.45, "ema_9_slope_3_atr": 0.3, "ema_20_slope_3_atr": 0.3},
                {"adx_14": 20.0, "ema_9_20_spread_atr": 0.70, "ema_9_slope_3_atr": 0.35, "ema_20_slope_3_atr": 0.35},
                {"adx_14": 21.0, "ema_9_20_spread_atr": 0.95, "ema_9_slope_3_atr": 0.4, "ema_20_slope_3_atr": 0.4},
            ]
        ),
        cfg,
    )
    falling = enrich_discovery_frame(
        _frame(
            [
                {"adx_14": 24.0, "ema_9_20_spread_atr": 0.10, "ema_9_slope_3_atr": 0.2, "ema_20_slope_3_atr": 0.2},
                {"adx_14": 23.5, "ema_9_20_spread_atr": 0.30, "ema_9_slope_3_atr": 0.25, "ema_20_slope_3_atr": 0.25},
                {"adx_14": 22.8, "ema_9_20_spread_atr": 0.55, "ema_9_slope_3_atr": 0.3, "ema_20_slope_3_atr": 0.3},
                {"adx_14": 22.0, "ema_9_20_spread_atr": 0.80, "ema_9_slope_3_atr": 0.35, "ema_20_slope_3_atr": 0.35},
                {"adx_14": 21.0, "ema_9_20_spread_atr": 1.10, "ema_9_slope_3_atr": 0.4, "ema_20_slope_3_atr": 0.4},
            ]
        ),
        cfg,
    )
    rising_rows = build_adx_asof_relationships(
        [_event(bar_index=3, event_type="ema_cross", direction="bullish", ts=rising.iloc[3]["decision_time"].isoformat(), close=100.0)],
        rising,
        cfg,
    )
    falling_rows = build_adx_asof_relationships(
        [_event(bar_index=3, event_type="ema_cross", direction="bullish", ts=falling.iloc[3]["decision_time"].isoformat(), close=100.0)],
        falling,
        cfg,
    )
    assert rising_rows[0]["adx_rising_streak_asof"] >= 2
    assert rising_rows[0]["adx_rising_into_ema_expansion"] is True
    assert falling_rows[0]["adx_falling_despite_ema_expansion"] is True
    assert falling_rows[0]["is_policy_feature"] is True
    assert "adx_rises_after_ema_cross_path" in falling_rows[0]


def test_ema_band_growing_shrinking_and_joint_slope() -> None:
    cfg = PatternDiscoveryC33BConfig()
    frame = enrich_discovery_frame(
        _frame(
            [
                {"ema_9_20_spread_atr": 0.05, "ema_9_slope_3_atr": 0.05, "ema_20_slope_3_atr": 0.02},
                {"ema_9_20_spread_atr": 0.10, "ema_9_slope_3_atr": 0.08, "ema_20_slope_3_atr": 0.05},
                {"ema_9_20_spread_atr": 0.18, "ema_9_slope_3_atr": 0.12, "ema_20_slope_3_atr": 0.10},
                {"ema_9_20_spread_atr": 0.40, "ema_9_slope_3_atr": 0.25, "ema_20_slope_3_atr": 0.22},
                {"ema_9_20_spread_atr": 0.20, "ema_9_slope_3_atr": 0.05, "ema_20_slope_3_atr": 0.04},
                {"ema_9_20_spread_atr": 0.08, "ema_9_slope_3_atr": 0.30, "ema_20_slope_3_atr": 0.28},
            ]
        ),
        cfg,
    )
    # bar3 band_change_3 = 0.40-0.05=0.35 grow; bar4 = 0.20-0.10=0.10 borderline;
    # force shrink/grow flags via explicit band_change after enrich for clarity.
    frame.loc[3, "band_change_3_atr"] = 0.25
    frame.loc[4, "band_change_3_atr"] = -0.20
    frame.loc[5, "ema_joint_slope_3_atr"] = 0.30
    events = [
        _event(bar_index=3, event_type="ema_cross", direction="bullish", ts=frame.iloc[3]["decision_time"].isoformat(), close=100.0, event_id="grow"),
        _event(bar_index=4, event_type="ema_cross", direction="bearish", ts=frame.iloc[4]["decision_time"].isoformat(), close=100.0, event_id="shrink"),
        _event(bar_index=5, event_type="ema_expansion_start", direction="bullish", ts=frame.iloc[5]["decision_time"].isoformat(), close=100.0, event_id="joint"),
    ]
    rows = {r["event_id"]: r for r in build_ema_band_dynamics(events, frame, cfg)}
    assert rows["grow"]["cross_with_growing_band"] is True
    assert rows["shrink"]["cross_with_shrinking_band"] is True
    assert rows["joint"]["joint_slope_without_cross"] is True


def test_multi_horizon_outcomes_and_early_adverse_recovery() -> None:
    cfg = PatternDiscoveryC33BConfig()

    clean = _frame([{ "close": 100.0 + i * 0.4, "high": 100.2 + i * 0.4, "low": 99.9 + i * 0.4 } for i in range(40)])
    delayed = _frame(
        [
            {"close": 100.0},
            *[{"close": 100.05 + i * 0.02, "high": 100.1 + i * 0.02, "low": 99.95 + i * 0.01} for i in range(1, 10)],
            *[{"close": 100.8 + i * 0.3, "high": 101.0 + i * 0.3, "low": 100.6 + i * 0.2} for i in range(10, 40)],
        ]
    )
    weak = _frame(
        [
            {
                "close": 100.0 + i * 0.015,
                "high": 100.02 + i * 0.015,
                "low": 99.65 if i % 3 == 0 else 99.95,
            }
            for i in range(40)
        ]
    )
    neutral = _frame([{ "close": 100.0 + (0.02 if i % 2 else -0.02), "high": 100.05, "low": 99.95 } for i in range(40)])
    early = _frame(
        [
            {"close": 100.0, "high": 100.2, "low": 99.9},
            {"close": 99.1, "high": 99.3, "low": 98.4},
            *[{"close": 99.5 + i * 0.35, "high": 99.8 + i * 0.35, "low": 99.3 + i * 0.3} for i in range(2, 40)],
        ]
    )
    failed = _frame([{ "close": 100.0, "high": 100.0, "low": 99.6 } for _ in range(40)])
    adverse = _frame([{ "close": 100.0 - i * 0.7, "high": 100.2 - i * 0.6, "low": 99.8 - i * 0.8 } for i in range(40)])
    mild = _frame(
        [
            {"close": 100.0, "high": 100.2, "low": 99.9},
            {"close": 99.45, "high": 99.6, "low": 99.35},  # MAE ~0.65: above clean, below early/adverse
            *[{"close": 100.2 + i * 0.25, "high": 100.5 + i * 0.25, "low": 100.0 + i * 0.2} for i in range(2, 40)],
        ]
    )

    cases = {
        "clean": (clean, "clean_success"),
        "delayed": (delayed, "delayed_success"),
        "weak": (weak, "weak_followthrough"),
        "neutral": (neutral, "neutral"),
        "early": (early, "early_adverse_then_recovery"),
        "failed": (failed, "failed_followthrough"),
        "adverse": (adverse, "adverse_reversal"),
        "mild": (mild, "clean_success"),
    }
    for name, (frame, expected) in cases.items():
        ev = {
            "event_id": name,
            "event_type": "ema_cross",
            "direction": "bullish",
            "event_timestamp": frame.iloc[0]["decision_time"].isoformat(),
            "bar_index": 0,
            "symbol": "APTUSDT",
            "timeframe": "30m",
            "close": 100.0,
        }
        out = compute_multi_horizon_outcomes_c33b([ev], frame, cfg)[0]
        assert out["outcome_class"] == expected, (name, out["outcome_class"])
        assert out["h3_evaluable"] is True
        assert out["h6_evaluable"] is True
        assert out["h12_evaluable"] is True
        assert out["h24_evaluable"] is True
        assert "h12_mfe_pct" in out
        assert "h12_mae_pct" in out
        assert "h12_directional_close_return_pct" in out


def test_component_flags_on_non_cross_events() -> None:
    cfg = PatternDiscoveryC33BConfig()
    frame = enrich_discovery_frame(
        _frame(
            [
                {
                    "adx_14": 22.0,
                    "ema_9_20_spread_atr": 0.5,
                    "ema_9_slope_3_atr": 0.3,
                    "ema_20_slope_3_atr": 0.25,
                    "di_spread": 2.0,
                    "close_to_ema_59_atr": 0.2,
                    "regime_proxy": "trend",
                }
                for _ in range(6)
            ]
        ),
        cfg,
    )
    # Force as-of deltas after enrich.
    frame.loc[4, "adx_delta_1"] = 0.5
    frame.loc[4, "adx_accel"] = 0.2
    frame.loc[4, "band_change_3_atr"] = 0.2
    frame.loc[4, "di_spread_abs_change_1"] = 0.5
    ev = {
        "event_id": "bo",
        "event_type": "range_breakout_confirmed",
        "direction": "bullish",
        "event_timestamp": frame.iloc[4]["decision_time"].isoformat(),
        "bar_index": 4,
        "lifecycle_stage": "confirmed",
        **frame.iloc[4].to_dict(),
    }
    flags = _policy_flags(ev, cfg)
    for key in (
        "has_di_cross",
        "has_di_expansion",
        "has_adx_level_confirmation",
        "has_adx_rising",
        "has_adx_acceleration",
        "has_ema_cross",
        "has_ema_joint_slope",
        "has_ema_band_expansion",
        "has_ema_band_compression",
        "has_ema59_context",
        "has_ema200_context",
        "has_breakout_context",
        "has_regime_proxy_context",
    ):
        assert key in flags
    assert flags["has_breakout_context"] is True
    assert flags["has_adx_level_confirmation"] is True
    assert flags["has_di_expansion"] is True
    assert flags["has_ema_band_expansion"] is True
    assert flags["has_regime_proxy_context"] is True


def test_candidate_selection_excludes_retrospective_and_bootstrap() -> None:
    cfg = PatternDiscoveryC33BConfig(
        min_pattern_events_discovery=2,
        min_pattern_events_validation=2,
        bootstrap_samples=50,
    )
    discovery = [
        {
            "pattern_id": "p1",
            "pattern_family": "f1",
            "sequence_family": "seq1",
            "sequence_core": "di_with_ema_follow",
            "outcome_class": "clean_success",
            "h12_directional_close_return_pct": 1.5,
            "h12_mfe_pct": 1.1,
            "split": "discovery",
            "is_policy_feature": True,
            "is_retrospective": False,
        },
        {
            "pattern_id": "p1",
            "pattern_family": "f1",
            "sequence_family": "seq1",
            "sequence_core": "di_with_ema_follow",
            "outcome_class": "weak_followthrough",
            "h12_directional_close_return_pct": 0.7,
            "h12_mfe_pct": 0.9,
            "split": "discovery",
            "is_policy_feature": True,
            "is_retrospective": False,
        },
        {
            "pattern_id": "p_retro",
            "pattern_family": "f_retro",
            "sequence_family": "di_leads_ema",
            "sequence_core": "di_leads_ema",
            "outcome_class": "clean_success",
            "h12_directional_close_return_pct": 2.0,
            "h12_mfe_pct": 2.0,
            "split": "discovery",
            "is_policy_feature": False,
            "is_retrospective": True,
        },
        {
            "pattern_id": "p_retro",
            "pattern_family": "f_retro",
            "sequence_family": "di_leads_ema",
            "sequence_core": "di_leads_ema",
            "outcome_class": "clean_success",
            "h12_directional_close_return_pct": 2.1,
            "h12_mfe_pct": 2.1,
            "split": "discovery",
            "is_policy_feature": False,
            "is_retrospective": True,
        },
        {
            "pattern_id": "p3",
            "pattern_family": "f3",
            "sequence_family": "seq3",
            "sequence_core": "ema_without_prior_di",
            "outcome_class": "clean_success",
            "h12_directional_close_return_pct": 1.2,
            "h12_mfe_pct": 1.2,
            "split": "discovery",
            "is_policy_feature": True,
            "is_retrospective": False,
        },
        {
            "pattern_id": "p3",
            "pattern_family": "f3",
            "sequence_family": "seq3",
            "sequence_core": "ema_without_prior_di",
            "outcome_class": "clean_success",
            "h12_directional_close_return_pct": 1.3,
            "h12_mfe_pct": 1.3,
            "split": "discovery",
            "is_policy_feature": True,
            "is_retrospective": False,
        },
    ]
    validation = [
        {
            "pattern_id": "p1",
            "pattern_family": "f1",
            "sequence_family": "seq1",
            "sequence_core": "di_with_ema_follow",
            "outcome_class": "clean_success",
            "h12_directional_close_return_pct": 1.4,
            "h12_mfe_pct": 1.0,
            "split": "validation",
            "is_policy_feature": True,
            "is_retrospective": False,
        },
        {
            "pattern_id": "p1",
            "pattern_family": "f1",
            "sequence_family": "seq1",
            "sequence_core": "di_with_ema_follow",
            "outcome_class": "clean_success",
            "h12_directional_close_return_pct": 1.6,
            "h12_mfe_pct": 1.2,
            "split": "validation",
            "is_policy_feature": True,
            "is_retrospective": False,
        },
        {
            "pattern_id": "p_retro",
            "pattern_family": "f_retro",
            "sequence_family": "di_leads_ema",
            "sequence_core": "di_leads_ema",
            "outcome_class": "clean_success",
            "h12_directional_close_return_pct": 2.0,
            "h12_mfe_pct": 2.0,
            "split": "validation",
            "is_policy_feature": False,
            "is_retrospective": True,
        },
        {
            "pattern_id": "p_retro",
            "pattern_family": "f_retro",
            "sequence_family": "di_leads_ema",
            "sequence_core": "di_leads_ema",
            "outcome_class": "clean_success",
            "h12_directional_close_return_pct": 2.2,
            "h12_mfe_pct": 2.2,
            "split": "validation",
            "is_policy_feature": False,
            "is_retrospective": True,
        },
        {
            "pattern_id": "p3",
            "pattern_family": "f3",
            "sequence_family": "seq3",
            "sequence_core": "ema_without_prior_di",
            "outcome_class": "failed_followthrough",
            "h12_directional_close_return_pct": -0.8,
            "h12_mfe_pct": -0.9,
            "split": "validation",
            "is_policy_feature": True,
            "is_retrospective": False,
        },
        {
            "pattern_id": "p3",
            "pattern_family": "f3",
            "sequence_family": "seq3",
            "sequence_core": "ema_without_prior_di",
            "outcome_class": "failed_followthrough",
            "h12_directional_close_return_pct": -1.0,
            "h12_mfe_pct": -1.1,
            "split": "validation",
            "is_policy_feature": True,
            "is_retrospective": False,
        },
    ]
    rows = build_candidate_patterns_c33b(discovery, validation, cfg)
    by_id = {row["pattern_id"]: row for row in rows}
    assert "p_retro" not in by_id
    assert by_id["p1"]["status"] == "research_candidate"
    assert by_id["p1"]["contains_retrospective_features"] is False
    assert by_id["p1"]["discovery_clean_rate_ci_low"] is not None
    assert "discovery" in by_id["p1"] and "validation" in by_id["p1"]
    assert by_id["p3"]["status"] == "unstable"
    assert by_id["p3"]["directional_sign_flip"] is True


def test_discovery_validation_split_causal() -> None:
    from research.regime_scanner.indicator_pattern_discovery import split_discovery_validation

    events = [
        {"event_id": "a", "event_timestamp": "2026-03-01T00:00:00+00:00", "pattern_id": "p"},
        {"event_id": "b", "event_timestamp": "2026-03-10T00:00:00+00:00", "pattern_id": "p"},
        {"event_id": "c", "event_timestamp": "2026-03-20T00:00:00+00:00", "pattern_id": "p"},
        {"event_id": "d", "event_timestamp": "2026-03-21T00:00:00+00:00", "pattern_id": "p"},
    ]
    split = split_discovery_validation(events, "2026-03-20")
    assert [e["event_id"] for e in split["discovery"]] == ["a", "b", "c"]
    assert [e["event_id"] for e in split["validation"]] == ["d"]


def test_pattern_ids_coarse_and_deterministic() -> None:
    cfg = PatternDiscoveryC33BConfig()
    row = {
        "sequence_core": "di_with_ema_follow",
        "adx_14": 22.0,
        "direction": "bullish",
        "sequence_family": "di_with_ema_follow__adx_20_25__rising__band_expand",
    }
    a = _assign_pattern_ids(dict(row), cfg)
    b = _assign_pattern_ids(dict(row), cfg)
    assert a["pattern_id"] == b["pattern_id"]
    assert a["pattern_id"] == "di_with_ema_follow__adx_20_25::bullish"
    assert a["pattern_family"] == "di_with_ema_follow__adx_20_25"


def test_component_ablation_and_sensitivity_rows() -> None:
    events = [
        {
            "pattern_id": "p1",
            "outcome_class": "clean_success",
            "h12_directional_close_return_pct": 1.5,
            "h12_mfe_pct": 1.2,
            "has_di_cross": True,
            "has_di_expansion": True,
            "has_adx_level_confirmation": True,
            "has_adx_rising": True,
            "has_adx_acceleration": True,
            "has_ema_cross": True,
            "has_ema_joint_slope": True,
            "has_ema_band_expansion": True,
            "has_ema_band_compression": False,
            "has_ema59_context": True,
            "has_ema200_context": False,
            "has_breakout_context": False,
            "has_regime_proxy_context": True,
            "h12_evaluable": True,
            "h12_mae_pct": 0.1,
            "band_change_3_atr": 0.2,
            "adx_14": 22.0,
        },
        {
            "pattern_id": "p2",
            "outcome_class": "failed_followthrough",
            "h12_directional_close_return_pct": -0.4,
            "h12_mfe_pct": 0.1,
            "has_di_cross": False,
            "has_di_expansion": False,
            "has_adx_level_confirmation": False,
            "has_adx_rising": False,
            "has_adx_acceleration": False,
            "has_ema_cross": False,
            "has_ema_joint_slope": False,
            "has_ema_band_expansion": False,
            "has_ema_band_compression": True,
            "has_ema59_context": False,
            "has_ema200_context": True,
            "has_breakout_context": True,
            "has_regime_proxy_context": True,
            "h12_evaluable": True,
            "h12_mae_pct": 1.1,
            "band_change_3_atr": -0.2,
            "adx_14": 12.0,
        },
    ]
    ablation = pattern_component_ablation(events, PatternDiscoveryC33BConfig())
    assert {row["component"] for row in ablation} >= {"has_di_cross", "has_ema_cross", "has_breakout_context"}
    sens = threshold_sensitivity_c33b(events, PatternDiscoveryC33BConfig())
    assert len(sens) == 8


def test_full_run_writes_required_artifacts_and_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    frame = enrich_discovery_frame(
        _frame(
            [
                {"di_spread": -2.0, "adx_14": 18.0, "ema_9_20_spread_atr": 0.10},
                {"di_spread": -1.0, "adx_14": 19.0, "ema_9_20_spread_atr": 0.20},
                {"di_spread": 1.2, "adx_14": 21.0, "ema_9_20_spread_atr": 0.35},
                {"di_spread": 1.8, "adx_14": 22.5, "ema_9_20_spread_atr": 0.50},
                {"di_spread": 2.1, "adx_14": 24.0, "ema_9_20_spread_atr": 0.70},
                {"di_spread": 2.4, "adx_14": 25.0, "ema_9_20_spread_atr": 0.90},
            ]
        ),
        PatternDiscoveryC33BConfig(),
    )
    events = [
        _event(bar_index=0, event_type="di_cross", direction="bullish", ts=frame.iloc[0]["decision_time"].isoformat(), close=100.0, event_id="e0"),
        _event(bar_index=1, event_type="ema_cross", direction="bullish", ts=frame.iloc[1]["decision_time"].isoformat(), close=100.2, event_id="e1"),
        _event(bar_index=2, event_type="di_cross", direction="bullish", ts=frame.iloc[2]["decision_time"].isoformat(), close=100.5, event_id="e2"),
        _event(bar_index=2, event_type="ema_cross", direction="bullish", ts=frame.iloc[2]["decision_time"].isoformat(), close=100.5, event_id="e3"),
        _event(bar_index=4, event_type="range_breakout_confirmed", direction="bullish", ts=frame.iloc[4]["decision_time"].isoformat(), close=101.2, event_id="e4"),
    ]

    monkeypatch.setattr(mod, "build_discovery_frame", lambda *args, **kwargs: frame.copy())
    monkeypatch.setattr(
        mod,
        "assert_baseline_readonly",
        lambda baseline_dir: {
            "baseline_dir": str(baseline_dir),
            "baseline_hash": C2_BASELINE_HASH,
            "expected_hash": C2_BASELINE_HASH,
            "hash_matches": True,
            "sha256sums_present": False,
        },
    )
    monkeypatch.setattr(mod, "detect_ema_crosses", lambda frame: [events[1], events[3]])
    monkeypatch.setattr(mod, "detect_di_crosses", lambda frame: [events[0], events[2]])
    monkeypatch.setattr(mod, "detect_ema_expansions", lambda frame: [])
    monkeypatch.setattr(mod, "detect_range_breakouts", lambda frame, cfg: [events[4]])
    monkeypatch.setattr(mod, "detect_trend_follow", lambda frame, cfg: [])

    summary = run_c33b_audit(
        output_dir=tmp_path,
        baseline_dir=tmp_path / "baseline",
        min_pattern_events=1,
        discovery_end="2026-03-02T00:00:00+00:00",
        load_start="2026-03-01",
        load_end="2026-03-03",
        analyze_start="2026-03-01",
        analyze_end="2026-03-03",
    )
    assert summary["baseline_hash_confirmed"] is True
    assert summary["baseline_reference_hash"] == C2_BASELINE_HASH
    assert summary["deterministic_hash"]
    assert summary["safety"]["no_classifier_changes"] is True
    assert summary["safety"]["no_production_config_changes"] is True
    for name in (
        "summary.json",
        "run_summary.json",
        "manifest.json",
        "events_enriched.csv",
        "di_ema_sequences.csv",
        "adx_asof_relationships.csv",
        "ema_band_dynamics.csv",
        "multi_horizon_outcomes.csv",
        "pattern_component_ablation.csv",
        "candidate_patterns_c3_3b.csv",
        "candidate_patterns_c3_3b.json",
        "threshold_sensitivity_c3_3b.csv",
        "indicator_combined_trend_detector_asof.pine",
        "indicator_combined_trend_detector_outcome_audit.pine",
        "trend_detector_state_counts.csv",
        "trend_detector_transitions.csv",
        "trend_detector_component_summary.csv",
    ):
        assert (tmp_path / name).exists()

    # Deterministic re-run.
    summary2 = run_c33b_audit(
        output_dir=tmp_path / "rerun",
        baseline_dir=tmp_path / "baseline",
        min_pattern_events=1,
        discovery_end="2026-03-02T00:00:00+00:00",
        load_start="2026-03-01",
        load_end="2026-03-03",
        analyze_start="2026-03-01",
        analyze_end="2026-03-03",
    )
    assert summary2["event_hash"] == summary["event_hash"]

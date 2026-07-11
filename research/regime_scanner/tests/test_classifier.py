"""Tests for the analytical regime / entry-risk classifier."""

from __future__ import annotations

import copy
import json
import math

import numpy as np
import pandas as pd
import pytest

from research.backtests.candle_loader import DEFAULT_DATA_DIR, symbol_to_feather_name
from research.regime_scanner.classifier import (
    classify_market_state,
    combine_timeframe_regimes,
    compute_overextension_score,
    summarize_timeframe_regime,
)
from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.point_audit import build_point_audit, json_safe


def _base_features(**overrides):
    slopes = {
        "9": {
            "3": {"current_slope": 0.5, "previous_slope": 0.4, "slope_change": 0.1, "status": "strengthening", "direction": "up"},
            "6": {"current_slope": 1.0, "previous_slope": 0.8, "slope_change": 0.2, "status": "strengthening", "direction": "up"},
            "12": {"current_slope": 2.0, "previous_slope": 1.0, "slope_change": 1.0, "status": "strengthening", "direction": "up"},
        },
        "20": {
            "12": {"current_slope": 2.0, "previous_slope": 0.5, "slope_change": 1.5, "status": "strengthening", "direction": "up"},
            "48": {"current_slope": 3.0, "previous_slope": 1.0, "slope_change": 2.0, "status": "strengthening", "direction": "up"},
        },
        "59": {
            "12": {"current_slope": 1.5, "previous_slope": 0.4, "slope_change": 1.1, "status": "strengthening", "direction": "up"},
            "48": {"current_slope": 2.5, "previous_slope": 0.8, "slope_change": 1.7, "status": "strengthening", "direction": "up"},
        },
        "200": {
            "48": {"current_slope": 1.2, "previous_slope": 0.5, "slope_change": 0.7, "status": "strengthening", "direction": "up"},
            "144": {"current_slope": 4.0, "previous_slope": 1.0, "slope_change": 3.0, "status": "strengthening", "direction": "up"},
        },
    }
    bands = {
        "ema_9_vs_ema_20": {
            "orientation": "bullish",
            "current_abs_pct": 1.0,
            "windows": {"12": {"status": "expanding", "current_abs_pct": 1.0, "previous_abs_pct": 0.3}},
        },
        "ema_20_vs_ema_59": {
            "orientation": "bullish",
            "current_abs_pct": 1.2,
            "windows": {"12": {"status": "expanding", "current_abs_pct": 1.2, "previous_abs_pct": 0.2}},
        },
        "ema_59_vs_ema_200": {
            "orientation": "bullish",
            "current_abs_pct": 2.0,
            "windows": {"12": {"status": "expanding", "current_abs_pct": 2.0, "previous_abs_pct": 1.0}},
        },
        "ema_9_vs_ema_200": {
            "orientation": "bullish",
            "current_abs_pct": 4.0,
            "windows": {"12": {"status": "expanding", "current_abs_pct": 4.0, "previous_abs_pct": 2.0}},
        },
    }
    features = {
        "candles_used": 2000,
        "warmup_sufficient": True,
        "ema": {"ema_9": 110.0, "ema_20": 108.0, "ema_59": 105.0, "ema_200": 100.0},
        "ema_order": "EMA9 > EMA20 > EMA59 > EMA200",
        "close_vs_ema_pct": {
            "close_vs_ema_9_pct": 0.5,
            "close_vs_ema_20_pct": 1.0,
            "close_vs_ema_59_pct": 2.0,
            "close_vs_ema_200_pct": 4.0,
        },
        "atr": 1.0,
        "atr_pct": 1.0,
        "plus_di": 40.0,
        "minus_di": 10.0,
        "di_spread": 30.0,
        "adx": 50.0,
        "ema_slope_comparisons": slopes,
        "ema_bands": bands,
        "overextension": {
            "close_vs_ema_atr_units": {"ema_9": 0.5, "ema_20": 0.8, "ema_59": 1.5, "ema_200": 3.0},
            "atr_pct_vs_means": {
                "12": {"ratio": 1.0, "label": "near_recent_volatility"},
                "48": {"ratio": 1.0, "label": "near_recent_volatility"},
                "144": {"ratio": 1.0, "label": "near_recent_volatility"},
            },
        },
        "confirmed_pivots": {
            "high_count": 5,
            "low_count": 5,
            "last_two_highs": [{"price": 108.0}, {"price": 111.0}],
            "last_two_lows": [{"price": 100.0}, {"price": 103.0}],
        },
        "confirmed_divergences": [
            {"status": "no_confirmed_divergence", "indicator": "adx"},
        ],
        "weakening_signals": [],
        "summary": {
            "adx_change": {"3": "rising", "6": "rising", "12": "rising"},
            "di_spread_change": {"3": "rising", "6": "rising", "12": "rising"},
        },
    }
    features.update(overrides)
    return features


def _classify(features: dict):
    return classify_market_state(**features, config=default_regime_scanner_config())


def test_strong_bullish_expansion_low_overextension() -> None:
    result = _classify(_base_features())
    assert result["regime"] == "strong_bullish_expansion"
    assert result["trend_strength_label"] in {"strong", "very_strong"}
    assert result["long_entry_risk"] in {"low", "moderate"}
    assert result["short_entry_risk"] in {"high", "extreme"}


def test_strong_bullish_high_overextension() -> None:
    feat = _base_features(
        overextension={
            "close_vs_ema_atr_units": {"ema_9": 2.0, "ema_20": 3.5, "ema_59": 6.5, "ema_200": 11.5},
            "atr_pct_vs_means": {
                "12": {"ratio": 1.3, "label": "above_recent_volatility"},
                "48": {"ratio": 1.2, "label": "above_recent_volatility"},
                "144": {"ratio": 1.1, "label": "above_recent_volatility"},
            },
        }
    )
    result = _classify(feat)
    assert result["regime"] == "strong_bullish_expansion"
    assert result["long_entry_risk"] in {"high", "extreme"}
    assert result["short_entry_risk"] == "extreme"
    codes = {c["code"] for c in result["reason_codes"]}
    assert "CLOSE_OVER_3_ATR_ABOVE_EMA20" in codes
    assert "CLOSE_OVER_10_ATR_ABOVE_EMA200" in codes


def test_bullish_with_short_term_deceleration_stays_expansion_or_trend() -> None:
    feat = _base_features()
    feat["ema_slope_comparisons"]["9"] = {
        "3": {"current_slope": 0.5, "previous_slope": 0.9, "slope_change": -0.4, "status": "weakening", "direction": "up"},
        "6": {"current_slope": 0.8, "previous_slope": 1.2, "slope_change": -0.4, "status": "weakening", "direction": "up"},
        "12": {"current_slope": 1.0, "previous_slope": 1.5, "slope_change": -0.5, "status": "weakening", "direction": "up"},
    }
    feat["weakening_signals"] = [
        {"type": "weakening_signal", "metric": "ema_9_slope_3", "lookback": 3},
    ]
    result = _classify(feat)
    assert result["regime"] in {"strong_bullish_expansion", "bullish_trend", "bullish_weakening"}
    codes = {c["code"] for c in result["reason_codes"]}
    assert "SHORT_TERM_SLOPE_DECELERATION" in codes
    # Short-term alone should not force decelerating if medium/long accelerate.
    assert result["acceleration_label"] in {"accelerating", "steady", "mixed"}


def test_bullish_with_confirmed_bearish_divergence() -> None:
    feat = _base_features(
        confirmed_divergences=[
            {"status": "confirmed_bearish_divergence", "indicator": "adx"},
        ],
        weakening_signals=[
            {"type": "weakening_signal", "metric": "plus_di", "lookback": 6},
            {"type": "weakening_signal", "metric": "di_spread", "lookback": 12},
            {"type": "weakening_signal", "metric": "ema_20_slope_12", "lookback": 12},
        ],
        summary={
            "adx_change": {"3": "falling", "6": "falling", "12": "stable"},
            "di_spread_change": {"3": "falling", "6": "falling", "12": "falling"},
        },
    )
    # Force medium-term deceleration as well.
    for period in ("20", "59"):
        for window, item in feat["ema_slope_comparisons"][period].items():
            item["status"] = "weakening"
            item["slope_change"] = -1.0
            item["previous_slope"] = item["current_slope"] + 1.0
    for pair in feat["ema_bands"].values():
        pair["windows"]["12"]["status"] = "contracting"
    result = _classify(feat)
    assert result["regime"] == "bullish_weakening"
    assert result["long_entry_risk"] in {"high", "extreme"}
    assert "CONFIRMED_BEARISH_DIVERGENCE" in {c["code"] for c in result["reason_codes"]}


def test_strong_bearish_expansion() -> None:
    feat = _base_features(
        ema={"ema_9": 90.0, "ema_20": 92.0, "ema_59": 95.0, "ema_200": 100.0},
        ema_order="EMA9 < EMA20 < EMA59 < EMA200",
        close_vs_ema_pct={
            "close_vs_ema_9_pct": -0.5,
            "close_vs_ema_20_pct": -1.0,
            "close_vs_ema_59_pct": -2.0,
            "close_vs_ema_200_pct": -4.0,
        },
        plus_di=10.0,
        minus_di=40.0,
        di_spread=-30.0,
        confirmed_pivots={
            "high_count": 5,
            "low_count": 5,
            "last_two_highs": [{"price": 110.0}, {"price": 105.0}],
            "last_two_lows": [{"price": 100.0}, {"price": 95.0}],
        },
        overextension={
            "close_vs_ema_atr_units": {"ema_9": -0.5, "ema_20": -0.8, "ema_59": -1.5, "ema_200": -3.0},
            "atr_pct_vs_means": {
                "12": {"ratio": 1.0, "label": "near_recent_volatility"},
                "48": {"ratio": 1.0, "label": "near_recent_volatility"},
                "144": {"ratio": 1.0, "label": "near_recent_volatility"},
            },
        },
    )
    # Flip slope directions to down / strengthening (more negative).
    for period, windows in feat["ema_slope_comparisons"].items():
        for window, item in windows.items():
            item["direction"] = "down"
            item["current_slope"] = -abs(float(item["current_slope"]))
            item["previous_slope"] = -abs(float(item["previous_slope"])) * 0.5
            item["status"] = "strengthening"
            item["slope_change"] = item["current_slope"] - item["previous_slope"]
    for pair in feat["ema_bands"].values():
        pair["orientation"] = "bearish"
    result = _classify(feat)
    assert result["regime"] == "strong_bearish_expansion"
    assert result["long_entry_risk"] in {"high", "extreme"}
    assert result["short_entry_risk"] in {"low", "moderate", "high"}


def test_bearish_high_short_overextension() -> None:
    feat = _base_features(
        ema={"ema_9": 90.0, "ema_20": 92.0, "ema_59": 95.0, "ema_200": 100.0},
        close_vs_ema_pct={
            "close_vs_ema_9_pct": -1.0,
            "close_vs_ema_20_pct": -2.0,
            "close_vs_ema_59_pct": -3.0,
            "close_vs_ema_200_pct": -5.0,
        },
        plus_di=8.0,
        minus_di=45.0,
        di_spread=-37.0,
        overextension={
            "close_vs_ema_atr_units": {"ema_9": -2.5, "ema_20": -3.5, "ema_59": -6.5, "ema_200": -11.0},
            "atr_pct_vs_means": {
                "12": {"ratio": 1.4, "label": "above_recent_volatility"},
                "48": {"ratio": 1.3, "label": "above_recent_volatility"},
                "144": {"ratio": 1.2, "label": "above_recent_volatility"},
            },
        },
        confirmed_pivots={
            "high_count": 4,
            "low_count": 4,
            "last_two_highs": [{"price": 110.0}, {"price": 104.0}],
            "last_two_lows": [{"price": 100.0}, {"price": 94.0}],
        },
    )
    for period, windows in feat["ema_slope_comparisons"].items():
        for item in windows.values():
            item["direction"] = "down"
            item["status"] = "strengthening"
            item["current_slope"] = -2.0
            item["previous_slope"] = -0.5
    for pair in feat["ema_bands"].values():
        pair["orientation"] = "bearish"
    result = _classify(feat)
    assert result["regime"] in {"strong_bearish_expansion", "bearish_trend"}
    assert result["short_entry_risk"] in {"high", "extreme"}
    assert "CLOSE_OVER_10_ATR_BELOW_EMA200" in {c["code"] for c in result["reason_codes"]}


def test_neutral_range() -> None:
    feat = _base_features(
        ema={"ema_9": 100.1, "ema_20": 100.0, "ema_59": 99.9, "ema_200": 100.05},
        ema_order="mixed",
        close_vs_ema_pct={
            "close_vs_ema_9_pct": 0.05,
            "close_vs_ema_20_pct": -0.02,
            "close_vs_ema_59_pct": 0.01,
            "close_vs_ema_200_pct": -0.03,
        },
        adx=12.0,
        di_spread=2.0,
        plus_di=18.0,
        minus_di=16.0,
        confirmed_pivots={
            "high_count": 3,
            "low_count": 3,
            "last_two_highs": [{"price": 101.0}, {"price": 100.8}],
            "last_two_lows": [{"price": 99.0}, {"price": 99.2}],
        },
        overextension={
            "close_vs_ema_atr_units": {"ema_9": 0.1, "ema_20": 0.0, "ema_59": 0.1, "ema_200": -0.1},
            "atr_pct_vs_means": {
                "12": {"ratio": 0.9, "label": "near_recent_volatility"},
                "48": {"ratio": 0.95, "label": "near_recent_volatility"},
                "144": {"ratio": 1.0, "label": "near_recent_volatility"},
            },
        },
        summary={
            "adx_change": {"3": "stable", "6": "stable", "12": "stable"},
            "di_spread_change": {"3": "stable", "6": "stable", "12": "stable"},
        },
    )
    for period, windows in feat["ema_slope_comparisons"].items():
        for item in windows.values():
            item["direction"] = "flat"
            item["status"] = "stable"
            item["current_slope"] = 0.0
            item["previous_slope"] = 0.0
            item["slope_change"] = 0.0
    for pair in feat["ema_bands"].values():
        pair["orientation"] = "flat"
        pair["windows"]["12"]["status"] = "stable"
    result = _classify(feat)
    assert result["regime"] in {"neutral_range", "transition"}


def test_transition_mixed_emas() -> None:
    feat = _base_features(
        ema={"ema_9": 110.0, "ema_20": 108.0, "ema_59": 95.0, "ema_200": 100.0},
        ema_order="mixed",
        close_vs_ema_pct={
            "close_vs_ema_9_pct": 1.0,
            "close_vs_ema_20_pct": 0.5,
            "close_vs_ema_59_pct": -1.0,
            "close_vs_ema_200_pct": 0.2,
        },
        adx=22.0,
        di_spread=4.0,
    )
    feat["ema_slope_comparisons"]["200"]["144"]["direction"] = "down"
    feat["ema_slope_comparisons"]["59"]["48"]["direction"] = "down"
    result = _classify(feat)
    assert result["regime"] in {"transition", "neutral_range", "bullish_trend"}


def test_low_adx_small_di_spread() -> None:
    feat = _base_features(adx=14.0, di_spread=3.0, plus_di=20.0, minus_di=17.0)
    result = _classify(feat)
    codes = {c["code"] for c in result["reason_codes"]}
    assert "ADX_WEAK" in codes
    assert "DI_SPREAD_NEAR_ZERO" in codes or result["regime"] in {"neutral_range", "transition", "bullish_trend"}


def test_insufficient_history_unavailable_or_transition() -> None:
    feat = _base_features(
        candles_used=20,
        warmup_sufficient=False,
        confirmed_pivots={"high_count": 0, "low_count": 0, "last_two_highs": [], "last_two_lows": []},
        ema={"ema_9": None, "ema_20": None, "ema_59": None, "ema_200": None},
        adx=None,
        di_spread=None,
    )
    result = _classify(feat)
    assert result["regime"] == "transition"
    assert result["long_entry_risk"] in {"unavailable", "low", "moderate", "high", "extreme"}
    assert "INSUFFICIENT_HISTORY" in {c["code"] for c in result["reason_codes"]}
    assert 0.0 <= result["confidence"] <= 1.0


def test_scores_finite_and_in_range() -> None:
    result = _classify(_base_features())
    scores = result["scores"]
    assert -100 <= scores["trend_direction_score"]["score"] <= 100
    assert 0 <= scores["trend_strength_score"]["score"] <= 100
    assert -100 <= scores["trend_acceleration_score"]["score"] <= 100
    assert 0 <= scores["overextension_score_long"]["score"] <= 100
    assert 0 <= scores["overextension_score_short"]["score"] <= 100
    assert 0 <= scores["reversal_risk_score_long"]["score"] <= 100
    assert 0 <= scores["reversal_risk_score_short"]["score"] <= 100
    assert 0 <= scores["data_quality_score"]["score"] <= 100
    for bundle in scores.values():
        assert math.isfinite(bundle["score"])
        for value in bundle["components"].values():
            assert math.isfinite(value)


def test_reason_codes_reproducible_and_deterministic() -> None:
    feat = _base_features()
    a = _classify(feat)
    b = _classify(copy.deepcopy(feat))
    assert a == b
    assert a["reason_codes"]
    assert all("code" in item and "explanation" in item for item in a["reason_codes"])


def test_future_candles_do_not_change_classification() -> None:
    start = pd.Timestamp("2026-01-13T10:00:00+00:00")
    rows = []
    price = 100.0
    for i in range(400):
        price = price * 1.0015
        rows.append(
            {
                "timestamp": start + pd.Timedelta(minutes=5 * i),
                "open": price * 0.999,
                "high": price * 1.002,
                "low": price * 0.998,
                "close": price,
                "volume": 1000.0,
            }
        )
    base = pd.DataFrame(rows)
    decision = base["timestamp"].iloc[-1] + pd.Timedelta(minutes=5)
    a = build_point_audit(symbol="SYN", decision_time=decision, candles=base)
    polluted = pd.concat(
        [
            base,
            pd.DataFrame(
                [
                    {
                        "timestamp": decision,
                        "open": 1.0,
                        "high": 500.0,
                        "low": 0.1,
                        "close": 250.0,
                        "volume": 1e9,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    b = build_point_audit(symbol="SYN", decision_time=decision, candles=polluted)
    assert a["classification"]["regime"] == b["classification"]["regime"]
    assert a["classification"]["scores"] == b["classification"]["scores"]
    assert a["classification"]["long_entry_risk"] == b["classification"]["long_entry_risk"]


@pytest.mark.skipif(
    not (DEFAULT_DATA_DIR / symbol_to_feather_name("APTUSDT")).exists(),
    reason="external APT feather file not present",
)
def test_apt_classification_integration() -> None:
    payload = build_point_audit(
        symbol="APTUSDT",
        decision_time="2026-01-13T23:00:00+00:00",
        history_candles=144,
    )
    assert payload["last_closed_candle"]["timestamp"] == "2026-01-13T22:55:00+00:00"
    clf = payload["classification"]
    codes = {c["code"] for c in clf["reason_codes"]}
    assert "EMA_FULL_BULLISH_ALIGNMENT" in codes
    assert clf["trend_strength_label"] in {"strong", "very_strong"}
    assert clf["scores"]["overextension_score_long"]["score"] >= default_regime_scanner_config().risk_moderate
    risk_rank = {"low": 0, "moderate": 1, "high": 2, "extreme": 3, "unavailable": -1}
    assert risk_rank[clf["short_entry_risk"]] > risk_rank[clf["long_entry_risk"]]
    assert "NO_CONFIRMED_BEARISH_DIVERGENCE" in codes
    assert "CONFIRMED_BEARISH_DIVERGENCE" not in codes
    encoded = json.dumps(json_safe(payload), allow_nan=False)
    assert "Infinity" not in encoded
    assert "NaN" not in encoded


def test_overextension_score_components_named() -> None:
    cfg = default_regime_scanner_config()
    oe = compute_overextension_score(
        side="long",
        overextension={
            "close_vs_ema_atr_units": {"ema_9": 1.0, "ema_20": 2.5, "ema_59": 5.0, "ema_200": 8.0},
            "atr_pct_vs_means": {"48": {"ratio": 1.2}},
        },
        config=cfg,
    )
    assert set(oe["components"]) == {"ema20_atr", "ema59_atr", "ema200_atr", "ema9_atr", "atr_pct_ratio"}


# --- Simple regime summary tests ---

def _bullish_base(**overrides) -> dict:
    payload = {
        "timeframe": "15m",
        "candles_loaded": 500,
        "warmup_sufficient": True,
        "ema": {"ema_9": 110.0, "ema_20": 108.0, "ema_59": 105.0, "ema_200": 100.0},
        "ema_order": "EMA9 > EMA20 > EMA59 > EMA200",
        "close_vs_ema_pct": {
            "close_vs_ema_9_pct": 0.4,
            "close_vs_ema_20_pct": 1.0,
            "close_vs_ema_59_pct": 2.0,
            "close_vs_ema_200_pct": 4.0,
        },
        "di_spread": 30.0,
        "adx": 50.0,
        "plus_di": 40.0,
        "minus_di": 10.0,
        "atr": 1.0,
        "atr_pct": 1.0,
        "ema_slopes_pct": {
            "ema_9_slope_3_pct": 0.5,
            "ema_9_slope_6_pct": 0.8,
            "ema_20_slope_6_pct": 1.0,
            "ema_20_slope_12_pct": 2.0,
            "ema_59_slope_48_pct": 2.5,
            "ema_200_slope_48_pct": 1.5,
            "ema_200_slope_144_pct": 3.0,
        },
        "ema_bands": {
            "ema_9_vs_ema_20": {
                "windows": {"12": {"status": "expanding"}},
            }
        },
        "confirmed_divergences": [],
        "confirmed_multi_metric_divergences": {"confirmed_bearish": [], "confirmed_bullish": []},
        "equal_high_retest_exhaustion": [],
        "lower_high_momentum_weakness": [],
        "developing_structural_exhaustion": None,
        "retest_high_candidate": None,
        "last_bar_rollover": {
            "adx_last_bar_rollover": False,
            "plus_di_last_bar_rollover": False,
            "di_spread_last_bar_rollover": False,
            "atr_pct_last_bar_rollover": False,
            "multi_metric_last_bar_rollover": False,
            "signals": [],
        },
        "weakening_signals": [],
    }
    payload.update(overrides)
    return payload


def _exhaustion_retest(*, confirmed: bool = False, multi: bool = True) -> dict:
    signals = [
        {"code": "ADX_EQUAL_HIGH_EXHAUSTION"},
        {"code": "ATR_EQUAL_HIGH_EXHAUSTION"},
        {"code": "PLUS_DI_EQUAL_HIGH_EXHAUSTION"},
    ]
    if multi:
        signals.append({"code": "MULTI_METRIC_EQUAL_HIGH_EXHAUSTION"})
    comps = {
        "adx": {"weakening": True, "percent_change": -25.0},
        "atr": {"weakening": True, "percent_change": -20.0},
        "atr_pct": {"weakening": True, "percent_change": -18.0},
        "plus_di": {"weakening": True, "percent_change": -15.0},
        "di_spread": {"weakening": True, "percent_change": -22.0},
    }
    status = (
        "confirmed_equal_high_exhaustion"
        if confirmed
        else "developing_equal_high_exhaustion"
    )
    return {
        "confirmation_status": status,
        "is_confirmed_pivot": confirmed,
        "structure": {"structure_type": "equal_high_exhaustion"},
        "signals": signals,
        "indicator_comparisons": comps,
    }


def test_strong_bullish_trend_without_weakness() -> None:
    result = summarize_timeframe_regime(_bullish_base())
    assert result["regime"] == "strong_bullish_trend"
    assert any(r["code"] == "NO_STRUCTURAL_WEAKNESS" for r in result["reason_codes"])


def test_bullish_with_developing_equal_high_exhaustion() -> None:
    retest = _exhaustion_retest(confirmed=False)
    payload = _bullish_base(
        developing_structural_exhaustion=retest,
        retest_high_candidate=retest,
    )
    result = summarize_timeframe_regime(payload)
    assert result["regime"] == "bullish_trend_with_trend_weakness"
    assert any(r["code"] == "DEVELOPING_EQUAL_HIGH_EXHAUSTION" for r in result["reason_codes"])
    assert any(r["code"] == "MULTI_METRIC_EXHAUSTION" for r in result["reason_codes"])


def test_bullish_with_confirmed_equal_high_exhaustion() -> None:
    retest = _exhaustion_retest(confirmed=True)
    payload = _bullish_base(
        equal_high_retest_exhaustion=[retest],
        retest_high_candidate=retest,
        developing_structural_exhaustion=None,
    )
    result = summarize_timeframe_regime(payload)
    assert result["regime"] == "bullish_trend_with_trend_weakness"
    assert any(r["code"] == "CONFIRMED_EQUAL_HIGH_EXHAUSTION" for r in result["reason_codes"])


def test_lone_last_bar_rollover_not_trend_weakness() -> None:
    payload = _bullish_base(
        last_bar_rollover={
            "adx_last_bar_rollover": False,
            "plus_di_last_bar_rollover": True,
            "di_spread_last_bar_rollover": True,
            "atr_pct_last_bar_rollover": True,
            "multi_metric_last_bar_rollover": True,
            "signals": [{"metric": "PLUS_DI_LAST_BAR_ROLLOVER"}],
        },
        weakening_signals=[{"metric": "PLUS_DI_LAST_BAR_ROLLOVER"}],
    )
    result = summarize_timeframe_regime(payload)
    assert result["regime"] in {"strong_bullish_trend", "bullish_trend"}
    assert result["regime"] != "bullish_trend_with_trend_weakness"


def test_multi_metric_exhaustion_forces_trend_weakness() -> None:
    retest = _exhaustion_retest(confirmed=False, multi=True)
    payload = _bullish_base(
        developing_structural_exhaustion=retest,
        retest_high_candidate=retest,
        adx=35.0,
    )
    result = summarize_timeframe_regime(payload)
    assert result["regime"] == "bullish_trend_with_trend_weakness"
    assert result["multi_metric_exhaustion"] is True


def test_bearish_trend_with_trend_weakness_mirror() -> None:
    payload = _bullish_base(
        ema={"ema_9": 90.0, "ema_20": 95.0, "ema_59": 100.0, "ema_200": 110.0},
        ema_order="EMA9 < EMA20 < EMA59 < EMA200",
        close_vs_ema_pct={
            "close_vs_ema_9_pct": -0.4,
            "close_vs_ema_20_pct": -1.0,
            "close_vs_ema_59_pct": -2.0,
            "close_vs_ema_200_pct": -4.0,
        },
        di_spread=-30.0,
        plus_di=10.0,
        minus_di=40.0,
        ema_slopes_pct={
            "ema_9_slope_3_pct": 0.2,
            "ema_20_slope_12_pct": -2.0,
            "ema_59_slope_48_pct": -2.5,
            "ema_200_slope_48_pct": -1.5,
        },
        confirmed_divergences=[
            {"status": "confirmed_bullish_divergence", "indicator": "adx"},
        ],
        confirmed_multi_metric_divergences={
            "confirmed_bearish": [],
            "confirmed_bullish": [{"status": "confirmed_bullish_divergence"}],
        },
        retest_high_candidate={
            "indicator_comparisons": {
                "adx": {"weakening": True, "percent_change": -20.0},
                "atr": {"weakening": True, "percent_change": -15.0},
                "atr_pct": {"weakening": True, "percent_change": -15.0},
                "plus_di": {"weakening": False, "percent_change": 5.0},
                "di_spread": {"weakening": True, "percent_change": -10.0},
            },
            "signals": [],
        },
    )
    result = summarize_timeframe_regime(payload)
    assert result["regime"] == "bearish_trend_with_trend_weakness"


def test_neutral_market() -> None:
    payload = _bullish_base(
        ema_order="EMA20 > EMA9 > EMA59 > EMA200",
        close_vs_ema_pct={
            "close_vs_ema_9_pct": 0.0,
            "close_vs_ema_20_pct": -0.1,
            "close_vs_ema_59_pct": 0.1,
            "close_vs_ema_200_pct": -0.05,
        },
        di_spread=1.0,
        adx=12.0,
        ema_slopes_pct={
            "ema_20_slope_12_pct": 0.0,
            "ema_59_slope_48_pct": None,
            "ema_200_slope_48_pct": None,
        },
    )
    result = summarize_timeframe_regime(payload)
    assert result["regime"] in {"neutral", "transition"}


def test_transition_mixed_emas() -> None:
    payload = _bullish_base(
        ema_order="EMA9 > EMA59 > EMA20 > EMA200",
        di_spread=8.0,
        adx=22.0,
        close_vs_ema_pct={
            "close_vs_ema_20_pct": 0.2,
            "close_vs_ema_59_pct": -0.2,
            "close_vs_ema_200_pct": 1.0,
        },
        ema_slopes_pct={
            "ema_20_slope_12_pct": 0.5,
            "ema_59_slope_48_pct": -0.5,
            "ema_200_slope_48_pct": 0.2,
        },
    )
    result = summarize_timeframe_regime(payload)
    assert result["regime"] in {"transition", "neutral"}


def test_insufficient_data_unavailable() -> None:
    result = summarize_timeframe_regime(
        {
            "timeframe": "15m",
            "candles_loaded": 0,
            "warmup_sufficient": False,
            "ema": None,
        }
    )
    assert result["regime"] == "unavailable"
    assert any(r["code"] == "INSUFFICIENT_DATA" for r in result["reason_codes"])


def test_deterministic_same_inputs() -> None:
    payload = _bullish_base()
    a = summarize_timeframe_regime(payload)
    b = summarize_timeframe_regime(copy.deepcopy(payload))
    assert a == b


def test_json_safe_regime_summary() -> None:
    payload = _bullish_base()
    result = summarize_timeframe_regime(payload)
    result["debug"] = math.inf
    safe = json_safe(result)
    encoded = json.dumps(safe, allow_nan=False)
    assert "Infinity" not in encoded
    assert "NaN" not in encoded


def test_future_data_does_not_change_combined_regime() -> None:
    start = "2026-01-13T18:00:00+00:00"
    rows = []
    for i in range(120):
        ts = pd.Timestamp(start) + pd.Timedelta(minutes=5 * i)
        px = 20 + math.sin(i / 5) * 0.5 + i * 0.01
        rows.append(
            {
                "timestamp": ts,
                "open": px,
                "high": px + 0.2,
                "low": px - 0.2,
                "close": px,
                "volume": 1.0,
            }
        )
    for i in range(12):
        ts = pd.Timestamp("2026-01-13T23:00:00+00:00") + pd.Timedelta(minutes=5 * i)
        rows.append(
            {
                "timestamp": ts,
                "open": 999.0,
                "high": 1000.0,
                "low": 998.0,
                "close": 999.5,
                "volume": 999.0,
            }
        )
    candles = pd.DataFrame(rows)
    decision = "2026-01-13T23:00:00+00:00"
    a = build_point_audit(
        symbol="SYN",
        decision_time=decision,
        candles=candles,
        timeframes="5m,15m,30m",
    )
    mutated = candles.copy()
    mutated.loc[mutated["timestamp"] >= pd.Timestamp(decision), ["high", "close"]] = 1e6
    b = build_point_audit(
        symbol="SYN",
        decision_time=decision,
        candles=mutated,
        timeframes="5m,15m,30m",
    )
    assert a["combined_regime"]["regime"] == b["combined_regime"]["regime"]
    assert json_safe(a["combined_regime"]["reason_codes"]) == json_safe(
        b["combined_regime"]["reason_codes"]
    )


@pytest.mark.skipif(
    not (DEFAULT_DATA_DIR / symbol_to_feather_name("APTUSDT")).exists(),
    reason="external APT feather file not present",
)
def test_apt_combined_regime_bullish_trend_with_trend_weakness() -> None:
    payload = build_point_audit(
        symbol="APTUSDT",
        decision_time="2026-01-13T23:00:00+00:00",
        history_candles=144,
        timeframes="5m,15m,30m",
    )
    combined = payload["combined_regime"]
    assert combined["regime"] == "bullish_trend_with_trend_weakness"
    assert combined["regime"] not in {
        "bearish_trend",
        "neutral",
        "strong_bullish_trend",
    }
    codes = {r["code"] for r in combined["reason_codes"]}
    assert "BULLISH_TREND_INTACT" in codes
    assert "MULTI_METRIC_EXHAUSTION" in codes or "DEVELOPING_EQUAL_HIGH_EXHAUSTION" in codes
    assert payload["by_timeframe"]["15m"]["developing_structural_exhaustion"][
        "confirmation_status"
    ] == "developing_equal_high_exhaustion"


def test_combine_promotes_multi_tf_weakness() -> None:
    retest = _exhaustion_retest(confirmed=False, multi=True)
    tf15 = _bullish_base(
        timeframe="15m",
        developing_structural_exhaustion=retest,
        retest_high_candidate=retest,
        adx=35.0,
    )
    tf5 = _bullish_base(
        timeframe="5m",
        last_bar_rollover={
            "adx_last_bar_rollover": False,
            "plus_di_last_bar_rollover": True,
            "di_spread_last_bar_rollover": True,
            "atr_pct_last_bar_rollover": True,
            "multi_metric_last_bar_rollover": True,
            "signals": [{"metric": "PLUS_DI_LAST_BAR_ROLLOVER"}],
        },
    )
    tf30 = _bullish_base(
        timeframe="30m",
        retest_high_candidate={
            "indicator_comparisons": {
                "adx": {"weakening": False, "percent_change": 2.0},
                "atr": {"weakening": False, "percent_change": 1.0},
                "atr_pct": {"weakening": False, "percent_change": 0.5},
                "plus_di": {"weakening": True, "percent_change": -19.0},
                "di_spread": {"weakening": True, "percent_change": -30.0},
            },
            "signals": [],
        },
    )
    combined = combine_timeframe_regimes({"5m": tf5, "15m": tf15, "30m": tf30})
    assert combined["regime"] == "bullish_trend_with_trend_weakness"
    assert any(r["code"] == "MULTI_TIMEFRAME_TREND_WEAKNESS" for r in combined["reason_codes"])

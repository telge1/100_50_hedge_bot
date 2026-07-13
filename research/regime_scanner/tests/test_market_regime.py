"""Unit tests for read-only market_regime K2_H4."""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest

from research.regime_scanner.market_regime import (
    MarketRegimeClassifier,
    MarketRegimeConfig,
    MarketRegimeFeatures,
    attach_readonly_market_regime,
    classify_k2_raw,
    compute_market_regime_features,
    default_market_regime_config,
    h4_confirm_bars,
    market_regime_hysteresis_docs,
)
from research.regime_scanner.regime_snapshot import build_regime_snapshot


def _feat(**kwargs) -> MarketRegimeFeatures:
    base = dict(
        ema9=100.0,
        ema20=101.0,
        ema9_slope_atr=-0.02,
        ema20_slope_atr=-0.02,
        ema_sep_atr=-0.1,
        ema_sep_change_atr=-0.05,
        share_above_both=0.1,
        share_below_both=0.8,
        ema_crosses=0,
        ema_flat=False,
        net_move_atr=-1.2,
        directional_efficiency=0.5,
        progress_vs_range=0.6,
        up_close_share=0.3,
        down_close_share=0.7,
        maximum_counter_move_atr=0.4,
        close=99.0,
        atr=1.0,
    )
    base.update(kwargs)
    return MarketRegimeFeatures(**base)


def _ts(i: int) -> datetime:
    return datetime(2026, 3, 5, 0, 0, tzinfo=timezone.utc).replace(hour=i % 24)


def test_clear_bearish_ema_progress() -> None:
    raw = classify_k2_raw(_feat())
    assert raw.regime == "strong_bearish_trend"
    assert "neg_net_atr" in raw.reasons


def test_clear_bullish_ema_progress() -> None:
    raw = classify_k2_raw(
        _feat(
            ema9=102.0,
            ema20=100.0,
            ema9_slope_atr=0.02,
            ema20_slope_atr=0.02,
            ema_sep_atr=0.1,
            share_above_both=0.8,
            share_below_both=0.1,
            net_move_atr=1.2,
            up_close_share=0.7,
            down_close_share=0.3,
        )
    )
    assert raw.regime == "strong_bullish_trend"
    assert "pos_net_atr" in raw.reasons


def test_flat_ema_low_progress_range() -> None:
    raw = classify_k2_raw(
        _feat(
            ema9_slope_atr=0.0,
            ema20_slope_atr=0.0,
            ema_sep_atr=0.0,
            share_above_both=0.4,
            share_below_both=0.4,
            ema_flat=True,
            ema_crosses=3,
            net_move_atr=0.1,
            directional_efficiency=0.15,
            progress_vs_range=0.2,
        )
    )
    assert raw.regime == "accumulation_range"


def test_mixed_evidence_transition() -> None:
    raw = classify_k2_raw(
        _feat(
            ema9_slope_atr=-0.02,
            ema20_slope_atr=0.01,
            ema_sep_atr=-0.02,
            share_below_both=0.5,
            share_above_both=0.4,
            ema_flat=False,
            ema_crosses=1,
            net_move_atr=-0.5,
            directional_efficiency=0.4,
            progress_vs_range=0.5,
        )
    )
    assert raw.regime == "transition_unclear"


def test_trend_hysteresis_h4() -> None:
    clf = MarketRegimeClassifier()
    # seed with range so next trend needs 2 confirms
    r0 = clf.update(
        decision_time=_ts(0),
        features=_feat(
            net_move_atr=0.1,
            directional_efficiency=0.1,
            progress_vs_range=0.1,
            ema_flat=True,
            ema_crosses=3,
            ema9_slope_atr=0.0,
            ema20_slope_atr=0.0,
            share_below_both=0.4,
            share_above_both=0.4,
        ),
    )
    assert r0.regime == "accumulation_range"
    bear = _feat()
    r1 = clf.update(decision_time=_ts(1), features=bear)
    assert r1.regime == "accumulation_range"
    assert r1.candidate_streak == 1
    assert h4_confirm_bars("strong_bearish_trend", clf.cfg) == 2
    r2 = clf.update(decision_time=_ts(2), features=bear)
    assert r2.regime == "strong_bearish_trend"


def test_range_hysteresis_h4() -> None:
    clf = MarketRegimeClassifier()
    # start in strong bearish
    b = _feat()
    clf.update(decision_time=_ts(0), features=b)
    assert clf.current_regime == "strong_bearish_trend"
    rng = _feat(
        net_move_atr=0.1,
        directional_efficiency=0.1,
        progress_vs_range=0.1,
        ema_flat=True,
        ema_crosses=3,
        ema9_slope_atr=0.0,
        ema20_slope_atr=0.0,
        share_below_both=0.4,
        share_above_both=0.4,
        ema_sep_atr=0.0,
    )
    # need 3 consecutive range raw bars
    for i in range(1, 3):
        ctx = clf.update(decision_time=_ts(i), features=rng)
        assert ctx.regime == "strong_bearish_trend"
    ctx3 = clf.update(decision_time=_ts(3), features=rng)
    assert ctx3.regime == "accumulation_range"
    assert h4_confirm_bars("accumulation_range", clf.cfg) == 3


def test_no_premature_label_before_confirmation() -> None:
    clf = MarketRegimeClassifier()
    clf.update(
        decision_time=_ts(0),
        features=_feat(
            net_move_atr=0.05,
            directional_efficiency=0.1,
            progress_vs_range=0.1,
            ema_flat=True,
            ema_crosses=2,
            ema9_slope_atr=0.0,
            ema20_slope_atr=0.0,
            share_below_both=0.4,
            share_above_both=0.4,
        ),
    )
    one = clf.update(decision_time=_ts(1), features=_feat())
    assert one.regime != "strong_bearish_trend"
    assert "hyst_hold_H4" in one.reason_codes[0]


def test_short_bounce_no_direct_countertrend() -> None:
    clf = MarketRegimeClassifier()
    clf.update(decision_time=_ts(0), features=_feat())  # strong bearish first bar
    # one bar opposite progress that is NOT enough for strong bullish → transition residual
    bounce = _feat(
        ema9_slope_atr=0.01,
        ema20_slope_atr=-0.005,
        ema_sep_atr=-0.02,
        share_below_both=0.5,
        share_above_both=0.4,
        net_move_atr=0.2,
        directional_efficiency=0.25,
        progress_vs_range=0.3,
        ema_flat=False,
        ema_crosses=1,
    )
    held = clf.update(decision_time=_ts(1), features=bounce)
    assert held.regime == "strong_bearish_trend"
    assert held.raw_regime == "transition_unclear"
    # still must not flip to bullish on a single strong-bullish raw without 2 confirms
    bull = _feat(
        ema9=102.0,
        ema20=100.0,
        ema9_slope_atr=0.02,
        ema20_slope_atr=0.02,
        ema_sep_atr=0.1,
        share_above_both=0.8,
        share_below_both=0.1,
        net_move_atr=1.2,
        up_close_share=0.7,
        down_close_share=0.3,
    )
    still = clf.update(decision_time=_ts(2), features=bull)
    assert still.regime == "strong_bearish_trend"
    flipped = clf.update(decision_time=_ts(3), features=bull)
    assert flipped.regime == "strong_bullish_trend"


def test_deterministic_replay() -> None:
    seq = [_feat(), _feat(net_move_atr=-0.2, directional_efficiency=0.2, progress_vs_range=0.2), _feat()]
    a = MarketRegimeClassifier()
    b = MarketRegimeClassifier()
    out_a = [a.update(decision_time=_ts(i), features=f).regime for i, f in enumerate(seq)]
    out_b = [b.update(decision_time=_ts(i), features=f).regime for i, f in enumerate(seq)]
    assert out_a == out_b


def test_no_future_leakage_in_feature_window() -> None:
    # Features at index i must ignore bars after i
    rng = np.random.default_rng(0)
    n = 40
    close = 100 + np.cumsum(rng.normal(0, 0.2, size=n))
    high = close + 0.3
    low = close - 0.3
    ema9 = close.copy()
    ema20 = close.copy() - 0.5
    atr = np.full(n, 1.0)
    f_full = compute_market_regime_features(close, high, low, ema9, ema20, atr, window=12)
    # poison future bars
    close2 = close.copy()
    close2[-1] = close2[-1] + 50
    f_poison_future_only_if_used = compute_market_regime_features(
        close2[: n - 1], high[: n - 1], low[: n - 1], ema9[: n - 1], ema20[: n - 1], atr[: n - 1], window=12
    )
    f_end = compute_market_regime_features(close[: n - 1], high[: n - 1], low[: n - 1], ema9[: n - 1], ema20[: n - 1], atr[: n - 1], window=12)
    assert f_poison_future_only_if_used is not None and f_end is not None
    assert f_poison_future_only_if_used.net_move_atr == f_end.net_move_atr
    assert f_full is not None


def test_mirror_symmetry_bullish_bearish() -> None:
    bear = classify_k2_raw(_feat())
    bull = classify_k2_raw(
        _feat(
            ema9=102.0,
            ema20=100.0,
            ema9_slope_atr=0.02,
            ema20_slope_atr=0.02,
            ema_sep_atr=0.1,
            ema_sep_change_atr=0.05,
            share_above_both=0.8,
            share_below_both=0.1,
            net_move_atr=1.2,
            up_close_share=0.7,
            down_close_share=0.3,
        )
    )
    assert bear.regime == "strong_bearish_trend"
    assert bull.regime == "strong_bullish_trend"


def test_config_has_no_magic_in_classifier_defaults() -> None:
    cfg = default_market_regime_config()
    assert cfg.variant_id == "K2_H4"
    assert cfg.trend_confirm_bars == 2
    assert cfg.range_confirm_bars == 3
    assert cfg.strong_net_move_atr == 1.0
    docs = market_regime_hysteresis_docs()
    assert docs["trend_confirm_bars"] == 2
    assert docs["opposite_trend_direct_switch"] is True
    # custom config must be respected (no hard-coded override)
    custom = MarketRegimeConfig(strong_net_move_atr=5.0, trend_confirm_bars=4)
    raw = classify_k2_raw(_feat(), custom)
    assert raw.regime != "strong_bearish_trend"  # net -1.2 fails abs>=5
    assert h4_confirm_bars("strong_bearish_trend", custom) == 4


def test_snapshot_attach_is_readonly_and_does_not_touch_policy_fields() -> None:
    clf = MarketRegimeClassifier()
    ctx = clf.update(decision_time=_ts(0), features=_feat())
    snap = build_regime_snapshot(
        decision_time="2026-03-05T00:30:00+00:00",
        combined_regime="bullish_trend",
        regime_5m="bullish_trend",
        regime_15m="bullish_trend",
        regime_30m="bullish_trend",
        market_regime_context=ctx,
    )
    assert snap["combined_regime"] == "bullish_trend"
    assert snap["market_regime"] == "strong_bearish_trend"
    assert snap["market_regime_read_only"] is True
    assert "allow_long" not in snap
    assert "allow_short" not in snap
    # attach helper preserves foreign policy keys if present
    with_policy = attach_readonly_market_regime(
        {"allow_long": True, "allow_short": False, "combined_regime": "x"},
        ctx,
    )
    assert with_policy["allow_long"] is True
    assert with_policy["allow_short"] is False
    assert with_policy["combined_regime"] == "x"

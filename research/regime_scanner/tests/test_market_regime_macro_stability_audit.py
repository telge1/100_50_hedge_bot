"""Unit tests for macro-stability post-processors (S0–S4)."""
from __future__ import annotations

from pathlib import Path

from research.regime_scanner.market_regime_macro_stability_audit import (
    BEAR_CONSOL,
    BEAR_TRENDING,
    BULL_CONSOL,
    BULL_TRENDING,
    POSSIBLE_REVERSAL,
    TRUE_RANGE,
    apply_s0,
    apply_s1,
    apply_s2,
    apply_s3,
    apply_s4,
    collapse_intervals,
    display_direction,
    metrics_for,
)


def _bar(regime: str, close: float = 1.0, high: float | None = None, low: float | None = None, i: int = 0):
    h = close + 0.01 if high is None else high
    lo = close - 0.01 if low is None else low
    import pandas as pd

    t0 = pd.Timestamp("2026-01-10", tz="UTC") + pd.Timedelta(hours=4 * i)
    return {
        "decision_time": t0 + pd.Timedelta(hours=4),
        "candle_open": t0,
        "regime": regime,
        "close": close,
        "high": h,
        "low": lo,
    }


def test_s0_mapping() -> None:
    tl = [
        _bar("strong_bullish_trend", i=0),
        _bar("strong_bearish_trend", i=1),
        _bar("accumulation_range", i=2),
        _bar("transition_unclear", i=3),
    ]
    assert apply_s0(tl) == [BULL_TRENDING, BEAR_TRENDING, TRUE_RANGE, POSSIBLE_REVERSAL]


def test_s1_maps_short_opposite_to_consolidating() -> None:
    # bear, bear, one bull, bear → bull bar should be bear consolidating under S1 (need 2 to flip)
    tl = [
        _bar("strong_bearish_trend", close=1.0, i=0),
        _bar("strong_bearish_trend", close=0.99, i=1),
        _bar("strong_bullish_trend", close=1.01, i=2),
        _bar("strong_bearish_trend", close=0.98, i=3),
    ]
    codes = apply_s1(tl)
    assert codes[0] == BEAR_TRENDING
    assert codes[2] == BEAR_CONSOL
    assert codes[3] == BEAR_TRENDING
    assert display_direction(codes[2]) == -1


def test_s2_needs_three_opposite_to_flip() -> None:
    tl = [
        _bar("strong_bearish_trend", i=0),
        _bar("strong_bullish_trend", i=1),
        _bar("strong_bullish_trend", i=2),
        _bar("strong_bullish_trend", i=3),
    ]
    codes = apply_s2(tl)
    assert codes[1] == BEAR_CONSOL
    assert codes[2] == BEAR_CONSOL
    assert codes[3] == BULL_TRENDING


def test_s3_possible_reversal_before_flip() -> None:
    tl = [
        _bar("strong_bearish_trend", i=0),
        _bar("strong_bullish_trend", i=1),
        _bar("strong_bullish_trend", i=2),
        _bar("strong_bullish_trend", i=3),
    ]
    codes = apply_s3(tl)
    assert codes[1] == BEAR_CONSOL
    assert codes[2] == POSSIBLE_REVERSAL
    assert codes[3] == BULL_TRENDING


def test_s4_blocks_flip_without_adverse_price() -> None:
    # opposite bars but close never breaks above the bearish run high
    tl = [
        _bar("strong_bearish_trend", close=1.00, high=1.02, low=0.99, i=0),
        _bar("strong_bullish_trend", close=1.005, high=1.015, low=1.00, i=1),
        _bar("strong_bullish_trend", close=1.008, high=1.016, low=1.00, i=2),
        _bar("strong_bullish_trend", close=1.010, high=1.017, low=1.00, i=3),
    ]
    codes = apply_s4(tl)
    assert BULL_TRENDING not in codes
    assert all(c in (BEAR_TRENDING, BEAR_CONSOL, POSSIBLE_REVERSAL) for c in codes)


def test_collapse_and_metrics_smoke() -> None:
    tl = [
        _bar("strong_bearish_trend", i=0),
        _bar("strong_bearish_trend", i=1),
        _bar("transition_unclear", i=2),
        _bar("strong_bullish_trend", i=3),
        _bar("strong_bullish_trend", i=4),
    ]
    s0 = apply_s0(tl)
    s1 = apply_s1(tl)
    iv = collapse_intervals(tl, s1)
    assert iv
    assert all("display_class" in r for r in iv)
    m = metrics_for("S1", tl, s1, iv, s0)
    assert m["direction_changes"] >= 0
    assert "focus_jan19_31" in m


def test_artifacts_exist_after_audit() -> None:
    out = Path("research/regime_scanner/results/market_regime_macro_stability_audit")
    if not (out / "summary.json").exists():
        return
    for v in ("s0", "s1", "s2", "s3", "s4"):
        pine = out / f"market_regime_macro_stability_{v}_2026_01.pine"
        assert pine.exists()
        text = pine.read_text()
        assert text.lstrip().startswith("//@version=6")
        assert "showLabels = input.bool(false" in text
        assert "ALIGNED" not in text
        assert "BOUNCE" not in text
        assert "box.new" not in text

"""Tests for C3.5c first-touch audit."""

from __future__ import annotations

import inspect

import numpy as np

from research.regime_scanner.pullback_entry_c3_5c_first_touch_audit import (
    DEFAULT_OUT,
    HORIZON_HOURS,
    TARGET_ADVERSE_COMBOS,
    TIMEFRAMES,
    classify_first_touch,
    horizon_bars_for_tf,
    horizons_for_tf,
)


def test_output_dir_and_scope() -> None:
    assert "c35c_first_touch_audit" in str(DEFAULT_OUT)
    assert TIMEFRAMES == ("5m", "15m")
    assert len(TARGET_ADVERSE_COMBOS) == 12
    assert 384 not in HORIZON_HOURS  # 16d excluded; max 8d=192h


def test_wall_clock_horizon_mapping() -> None:
    b5, a5 = horizon_bars_for_tf("5m", 24)
    assert b5 == 288 and a5 == 24.0
    b15, a15 = horizon_bars_for_tf("15m", 24)
    assert b15 == 96 and a15 == 24.0
    labels = [h["label"] for h in horizons_for_tf("5m")]
    assert labels == ["6h", "12h", "24h", "48h", "4d", "8d"]


def test_long_target_first() -> None:
    highs = np.array([100.2, 101.2, 100.5])
    lows = np.array([99.8, 100.0, 99.9])
    m = classify_first_touch(
        side=1,
        entry_price=100.0,
        highs=highs,
        lows=lows,
        timestamps=[0, 1, 2],
        fill_bar=0,
        horizon_bars=3,
        n_bars=3,
        target_pct=1.0,
        adverse_pct=0.5,
    )
    assert m["outcome"] == "target_first"
    assert m["bars_to_first_touch"] == 1


def test_short_adverse_first_mirrored() -> None:
    highs = np.array([100.6, 100.2, 99.0])
    lows = np.array([99.9, 99.8, 98.5])
    m = classify_first_touch(
        side=-1,
        entry_price=100.0,
        highs=highs,
        lows=lows,
        timestamps=[0, 1, 2],
        fill_bar=0,
        horizon_bars=3,
        n_bars=3,
        target_pct=1.0,
        adverse_pct=0.5,
    )
    assert m["outcome"] == "adverse_first"
    assert m["bars_to_first_touch"] == 0


def test_ambiguous_same_bar() -> None:
    highs = np.array([101.5])
    lows = np.array([98.5])
    m = classify_first_touch(
        side=1,
        entry_price=100.0,
        highs=highs,
        lows=lows,
        timestamps=[0],
        fill_bar=0,
        horizon_bars=1,
        n_bars=1,
        target_pct=1.0,
        adverse_pct=1.0,
    )
    assert m["outcome"] == "ambiguous_same_bar"
    assert m["same_bar_target_hit"] is True


def test_neither() -> None:
    highs = np.array([100.2, 100.3])
    lows = np.array([99.8, 99.7])
    m = classify_first_touch(
        side=1,
        entry_price=100.0,
        highs=highs,
        lows=lows,
        timestamps=[0, 1],
        fill_bar=0,
        horizon_bars=2,
        n_bars=2,
        target_pct=1.0,
        adverse_pct=0.5,
    )
    assert m["outcome"] == "neither"


def test_fill_bar_included_no_pre_fill() -> None:
    # Target hit only on fill bar (loc 0)
    highs = np.array([101.5, 100.1])
    lows = np.array([100.0, 99.9])
    m = classify_first_touch(
        side=1,
        entry_price=100.0,
        highs=highs,
        lows=lows,
        timestamps=[10, 11],
        fill_bar=0,
        horizon_bars=2,
        n_bars=2,
        target_pct=1.0,
        adverse_pct=0.5,
    )
    assert m["bars_to_first_touch"] == 0
    assert m["first_touch_timestamp"] == 10


def test_incomplete_horizon_flag() -> None:
    highs = np.array([100.1, 100.2])
    lows = np.array([99.9, 99.8])
    m = classify_first_touch(
        side=1,
        entry_price=100.0,
        highs=highs,
        lows=lows,
        timestamps=[0, 1],
        fill_bar=0,
        horizon_bars=10,
        n_bars=2,
        target_pct=5.0,
        adverse_pct=5.0,
    )
    assert m["incomplete_horizon"] is True
    assert m["outcome"] == "neither"


def test_conservative_expectancy_formula() -> None:
    # manual: wr=0.4, target=1, adverse=0.5 → 0.4*1 - 0.6*0.5 = 0.1
    wr = 0.4
    exp = wr * 1.0 - (1 - wr) * 0.5
    assert abs(exp - 0.1) < 1e-12


def test_no_lookahead_source() -> None:
    import research.regime_scanner.pullback_entry_c3_5c_first_touch_audit as mod

    src = inspect.getsource(mod)
    assert "lookahead_on" not in src
    assert "shift(-" not in src

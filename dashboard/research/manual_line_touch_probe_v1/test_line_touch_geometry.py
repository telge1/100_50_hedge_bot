"""Unit tests for manual line touch geometry (Phase T1)."""

from __future__ import annotations

import math

try:
    from .line_geometry import LineDef, classify_distance, classify_candle_interval, distance_pct_at_open
except ImportError:
    from line_geometry import LineDef, classify_distance, classify_candle_interval, distance_pct_at_open


def _trend(t1, p1, t2, p2, extend=False):
    return LineDef(
        drawing_id="d-test",
        drawing_type="trend",
        symbol="DOGEUSDT",
        extend_right=extend,
        t1=t1,
        p1=p1,
        t2=t2,
        p2=p2,
    )


def _hline(price):
    return LineDef(
        drawing_id="d-h",
        drawing_type="hline",
        symbol="DOGEUSDT",
        extend_right=False,
        price=price,
    )


def test_hline_touch_through_range():
    line = _hline(0.10)
    cls, _, dist, _, touch = classify_candle_interval(line, 1000.0, 0.11, 0.09)
    assert touch and cls == "TOUCH"
    assert dist == 0.0


def test_diagonal_touch_through_range():
    line = _trend(0, 0.10, 1000, 0.20)
    cls, _, dist, method, touch = classify_candle_interval(line, 400, 0.16, 0.14)
    assert touch and cls == "TOUCH"
    assert method == "interval"
    assert dist == 0.0


def test_exact_10pct_near():
    lp = 1.0
    low, high = 1.001, 1.02
    d = distance_pct_at_open(lp, low, high)
    assert math.isclose(d, 0.10, rel_tol=0, abs_tol=1e-9)
    assert classify_distance(d, False) == "NEAR_0_10"


def test_exact_20pct_near():
    lp = 1.0
    low, high = 1.002, 1.03
    d = distance_pct_at_open(lp, low, high)
    assert math.isclose(d, 0.20, rel_tol=0, abs_tol=1e-9)
    assert classify_distance(d, False) == "NEAR_0_20"


def test_beyond_20_no_touch():
    lp = 1.0
    low, high = 1.03, 1.04
    d = distance_pct_at_open(lp, low, high)
    assert d > 0.20
    assert classify_distance(d, False) == "NO_TOUCH"


def test_extend_right_active_after_second_anchor():
    line = _trend(1000, 0.10, 2000, 0.12, extend=True)
    assert line.active_for_candle(2500)
    assert line.anchor_status(2500) == "ON_EXTENDED_RAY"
    p = line.price_at(2500)
    assert p is not None and p > 0.12


def test_finite_segment_inactive_after_end():
    line = _trend(1000, 0.10, 2000, 0.12, extend=False)
    assert not line.active_for_candle(3000)
    cls, _, _, method, _ = classify_candle_interval(line, 3000, 0.11, 0.10)
    assert cls == "NO_TOUCH" and method == "inactive"


def test_no_epoch_zero_timestamps():
    line = _trend(1_700_000_000, 0.08, 1_700_001_000, 0.09)
    assert line.t1 > 1e9 and line.t2 > 1e9

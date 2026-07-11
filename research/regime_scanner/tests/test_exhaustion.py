"""Tests for equal-high / retest exhaustion and lower-high weakness."""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from research.regime_scanner.config import RegimeScannerConfig, default_regime_scanner_config
from research.regime_scanner.exhaustion import (
    classify_high_structure,
    compare_indicator,
    detect_structural_exhaustion,
    find_retest_high_candidate,
)
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.swings import ConfirmedPivot


def _frame_from_highs(
    highs: list[float],
    *,
    start: str = "2026-01-13T00:00:00+00:00",
    interval_minutes: int = 15,
    indicators: dict[int, dict[str, float]] | None = None,
) -> pd.DataFrame:
    start_ts = pd.Timestamp(start)
    rows = []
    for i, high in enumerate(highs):
        ts = start_ts + pd.Timedelta(minutes=interval_minutes * i)
        base = float(high) - 0.01
        rows.append(
            {
                "timestamp": ts,
                "open": base,
                "high": float(high),
                "low": base - 0.02,
                "close": base,
                "volume": 10.0,
                "adx": 30.0,
                "atr": 1.0,
                "atr_pct": 1.0,
                "plus_di": 20.0,
                "minus_di": 10.0,
                "di_spread": 10.0,
            }
        )
    frame = pd.DataFrame(rows)
    if indicators:
        for idx, values in indicators.items():
            for col, value in values.items():
                frame.loc[idx, col] = value
    return frame


def _pivot(index: int, price: float, frame: pd.DataFrame) -> ConfirmedPivot:
    conf_i = min(index + 2, len(frame) - 1)
    return ConfirmedPivot(
        pivot_index=index,
        pivot_timestamp=pd.Timestamp(frame.iloc[index]["timestamp"]).isoformat(),
        confirmation_index=conf_i,
        confirmation_timestamp=pd.Timestamp(frame.iloc[conf_i]["timestamp"]).isoformat(),
        price=float(price),
        pivot_type="high",
    )


def test_exact_equal_high_weaker_adx_atr() -> None:
    cfg = default_regime_scanner_config().with_timeframe("15m")
    frame = _frame_from_highs(
        [1.0] * 8 + [2.0031] + [1.9] * 4 + [2.0031] + [1.95, 1.94],
        indicators={
            8: {"adx": 50.0, "atr": 2.0, "atr_pct": 2.0, "plus_di": 40.0, "di_spread": 30.0},
            13: {"adx": 30.0, "atr": 1.0, "atr_pct": 1.0, "plus_di": 20.0, "di_spread": 10.0},
        },
    )
    pivots = [_pivot(8, 2.0031, frame)]
    # Candidate at 13 is not confirmed.
    out = find_retest_high_candidate(frame, pivots, config=cfg)
    assert out is not None
    assert out["structure"]["structure_type"] == "equal_high_exhaustion"
    assert out["structure"]["price_direction"] == "equal"
    assert out["confirmation_status"] == "developing_equal_high_exhaustion"
    codes = {s["code"] for s in out["signals"]}
    assert "ADX_EQUAL_HIGH_EXHAUSTION" in codes
    assert "ATR_EQUAL_HIGH_EXHAUSTION" in codes
    assert "MULTI_METRIC_EQUAL_HIGH_EXHAUSTION" in codes
    assert "confirmed_higher_high_divergence" not in str(out)
    assert "confirmed_bearish_divergence" not in out["confirmation_status"]


def test_retest_within_025_pct() -> None:
    first = 2.0
    second = 2.0 * (1.0 - 0.0020)  # 0.20% lower
    structure = classify_high_structure(first, second, config=default_regime_scanner_config())
    assert structure["tolerance_match"]["0.25"] is True
    assert structure["tolerance_match"]["0.1"] is False
    assert structure["structure_type"] == "equal_high_exhaustion"
    assert structure["price_direction"] == "slightly_lower"


def test_outside_025_pct_not_retest_for_that_tol() -> None:
    first = 2.0
    second = 2.0 * (1.0 - 0.0030)  # 0.30% lower
    structure = classify_high_structure(first, second, config=default_regime_scanner_config())
    assert structure["tolerance_match"]["0.25"] is False
    assert structure["tolerance_match"]["0.5"] is True
    # Still equal_high under default equal_high_tolerance_pct=0.50
    assert structure["structure_type"] == "equal_high_exhaustion"
    tight = RegimeScannerConfig(equal_high_tolerance_pct=0.25, lower_high_retest_tolerance_pct=0.25)
    structure_tight = classify_high_structure(first, second, config=tight)
    assert structure_tight["structure_type"] == "outside_retest_zone"


def test_retest_weaker_adx_stronger_atr() -> None:
    cfg = default_regime_scanner_config().with_timeframe("15m")
    frame = _frame_from_highs(
        [1.0] * 6 + [2.0] + [1.9] * 3 + [1.999] + [1.95, 1.94],
        indicators={
            6: {"adx": 40.0, "atr": 1.0, "atr_pct": 1.0, "plus_di": 25.0, "di_spread": 15.0},
            10: {"adx": 20.0, "atr": 1.5, "atr_pct": 1.5, "plus_di": 24.0, "di_spread": 14.5},
        },
    )
    pivots = [_pivot(6, 2.0, frame)]
    out = find_retest_high_candidate(frame, pivots, config=cfg)
    assert out is not None
    assert out["indicator_comparisons"]["adx"]["weakening"] is True
    assert out["indicator_comparisons"]["atr"]["weakening"] is False
    codes = {s["code"] for s in out["signals"]}
    assert "ADX_EQUAL_HIGH_EXHAUSTION" in codes
    assert "ATR_EQUAL_HIGH_EXHAUSTION" not in codes


def test_multi_metric_requires_two_families() -> None:
    cfg = default_regime_scanner_config().with_timeframe("15m")
    frame = _frame_from_highs(
        [1.0] * 6 + [2.0] + [1.9] * 3 + [2.0] + [1.95, 1.94],
        indicators={
            6: {"adx": 40.0, "atr": 1.0, "atr_pct": 1.0, "plus_di": 20.0, "di_spread": 10.0},
            # Only ADX clearly weaker; ATR/+DI/DI barely change.
            10: {"adx": 20.0, "atr": 0.99, "atr_pct": 0.99, "plus_di": 19.5, "di_spread": 9.8},
        },
    )
    pivots = [_pivot(6, 2.0, frame)]
    out = find_retest_high_candidate(frame, pivots, config=cfg)
    assert out is not None
    codes = {s["code"] for s in out["signals"]}
    assert "ADX_EQUAL_HIGH_EXHAUSTION" in codes
    assert "MULTI_METRIC_EQUAL_HIGH_EXHAUSTION" not in codes

    frame.loc[10, "atr"] = 0.7
    frame.loc[10, "atr_pct"] = 0.7
    out2 = find_retest_high_candidate(frame, pivots, config=cfg)
    codes2 = {s["code"] for s in out2["signals"]}
    assert "MULTI_METRIC_EQUAL_HIGH_EXHAUSTION" in codes2


def test_developing_retest_without_right_confirmation() -> None:
    cfg = default_regime_scanner_config().with_timeframe("15m")
    frame = _frame_from_highs(
        [1.0] * 6 + [2.0] + [1.9] * 3 + [1.998],
        indicators={
            6: {"adx": 50.0, "atr": 2.0, "atr_pct": 2.0, "plus_di": 40.0, "di_spread": 30.0},
            10: {"adx": 25.0, "atr": 1.0, "atr_pct": 1.0, "plus_di": 15.0, "di_spread": 5.0},
        },
    )
    pivots = [_pivot(6, 2.0, frame)]
    pack = detect_structural_exhaustion(frame, pivots, timeframe="15m", config=cfg)
    assert pack["developing_structural_exhaustion"] is not None
    assert pack["developing_structural_exhaustion"]["confirmation_status"].startswith(
        "developing_"
    )
    assert pack["developing_structural_exhaustion"]["available_right_candles"] == 0
    assert pack["developing_structural_exhaustion"]["required_right_candles"] == 2


def test_same_candidate_becomes_confirmed_after_right_bars() -> None:
    cfg = default_regime_scanner_config().with_timeframe("15m")
    highs = [1.0] * 6 + [2.0] + [1.9] * 3 + [1.998] + [1.95, 1.94]
    frame = _frame_from_highs(
        highs,
        indicators={
            6: {"adx": 50.0, "atr": 2.0, "atr_pct": 2.0, "plus_di": 40.0, "di_spread": 30.0},
            10: {"adx": 25.0, "atr": 1.0, "atr_pct": 1.0, "plus_di": 15.0, "di_spread": 5.0},
        },
    )
    pivots = [_pivot(6, 2.0, frame), _pivot(10, 1.998, frame)]
    out = find_retest_high_candidate(frame, pivots, config=cfg)
    assert out is not None
    assert out["is_confirmed_pivot"] is True
    assert out["confirmation_status"] == "confirmed_equal_high_exhaustion"
    pack = detect_structural_exhaustion(frame, pivots, timeframe="15m", config=cfg)
    assert pack["developing_structural_exhaustion"] is None
    assert pack["equal_high_retest_exhaustion"]


def test_no_future_candle_usage() -> None:
    cfg = default_regime_scanner_config().with_timeframe("15m")
    frame = _frame_from_highs(
        [1.0] * 6 + [2.0] + [1.9] * 2 + [1.997] + [9.0, 9.0],
        indicators={
            6: {"adx": 40.0, "atr": 2.0, "atr_pct": 2.0, "plus_di": 30.0, "di_spread": 20.0},
            9: {"adx": 20.0, "atr": 1.0, "atr_pct": 1.0, "plus_di": 10.0, "di_spread": 5.0},
            10: {"adx": 5.0, "atr": 0.1, "atr_pct": 0.1, "plus_di": 1.0, "di_spread": 1.0},
        },
    )
    # Decision cuts before future spike bars 10/11.
    closed = frame.iloc[:10].copy().reset_index(drop=True)
    pivots = [_pivot(6, 2.0, closed)]
    out = find_retest_high_candidate(closed, pivots, config=cfg)
    assert out is not None
    assert out["candidate_index"] == 9
    assert out["candidate_price"] == pytest.approx(1.997)
    # Window max must not include future bar 10.
    windows = out["indicator_comparisons"]["adx"]["windows"]["max_pm1"]
    assert windows["candidate_value"] != pytest.approx(5.0)


def test_indicator_max_one_candle_before_price_high() -> None:
    cfg = default_regime_scanner_config().with_timeframe("15m")
    frame = _frame_from_highs(
        [1.0] * 6 + [2.0] + [1.9] * 2 + [1.95, 1.998] + [1.94, 1.93],
        indicators={
            6: {"adx": 40.0, "atr": 2.0, "atr_pct": 2.0, "plus_di": 30.0, "di_spread": 20.0},
            9: {"adx": 35.0, "atr": 1.8, "atr_pct": 1.8, "plus_di": 18.0, "di_spread": 12.0},
            10: {"adx": 20.0, "atr": 1.0, "atr_pct": 1.0, "plus_di": 10.0, "di_spread": 5.0},
        },
    )
    pivots = [_pivot(6, 2.0, frame)]
    out = find_retest_high_candidate(frame, pivots, config=cfg)
    assert out is not None
    assert out["candidate_index"] == 10
    exact = out["indicator_comparisons"]["adx"]["reference_value"]
    win = out["indicator_comparisons"]["adx"]["windows"]["max_pm1"]
    # Reference window can pick neighboring max; candidate ±1 includes bar 9.
    assert win["candidate_value"] == pytest.approx(35.0)
    assert exact == pytest.approx(40.0)
    cmp = compare_indicator(
        frame,
        reference_index=6,
        candidate_index=10,
        metric="adx",
        config=cfg,
    )
    assert cmp["windows"]["max_pm1"]["candidate_value"] == pytest.approx(35.0)


def test_json_safe_exhaustion_payload() -> None:
    cfg = default_regime_scanner_config().with_timeframe("15m")
    frame = _frame_from_highs(
        [1.0] * 6 + [2.0] + [1.9] * 3 + [2.0] + [1.95, 1.94],
        indicators={
            6: {"adx": 50.0, "atr": 2.0, "atr_pct": 2.0, "plus_di": 40.0, "di_spread": 30.0},
            10: {"adx": 20.0, "atr": 1.0, "atr_pct": 1.0, "plus_di": 10.0, "di_spread": 5.0},
        },
    )
    pivots = [_pivot(6, 2.0, frame)]
    pack = detect_structural_exhaustion(frame, pivots, timeframe="15m", config=cfg)
    # Inject non-finite to ensure sanitizer path in consumers.
    pack["debug_inf"] = math.inf
    safe = json_safe(pack)
    encoded = json.dumps(safe, allow_nan=False)
    assert "Infinity" not in encoded
    assert "NaN" not in encoded
    assert safe["mark_price_note"]


def test_mark_price_documented_not_simulated() -> None:
    cfg = default_regime_scanner_config()
    assert "not simulated" in cfg.mark_price_deviation_note.lower()
    frame = _frame_from_highs([1.0] * 6 + [2.0] + [1.9] * 3 + [1.999])
    pivots = [_pivot(6, 2.0, frame)]
    out = find_retest_high_candidate(frame, pivots, config=cfg.with_timeframe("15m"))
    assert out is not None
    assert "not simulated" in out["mark_price_note"].lower()
    # No synthetic mark column is invented.
    assert "mark" not in frame.columns

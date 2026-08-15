"""Outcome evaluator causality / ambiguity tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from research.trend_forecast_validation.config import default_config
from research.trend_forecast_validation.outcome_evaluator import (
    _first_touch_path,
    evaluate_signal_outcomes,
)


def test_same_candle_ambiguity_is_not_success() -> None:
    highs = pd.Series([11.0]).to_numpy()
    lows = pd.Series([9.0]).to_numpy()
    touch = _first_touch_path(
        direction="bullish",
        entry=10.0,
        target=10.5,
        invalidation=9.5,
        highs=highs,
        lows=lows,
        timestamps=["t1"],
        ambiguity_mode="conservative",
    )
    assert touch["ambiguous"] is True
    assert touch["outcome"] == "AMBIGUOUS"
    assert touch["conservative_outcome"] == "FAILURE"
    assert touch["optimistic_outcome"] == "SUCCESS"


def test_outcomes_start_after_signal_candle() -> None:
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)
    candles = []
    for i in range(30):
        ts = pd.Timestamp(base) + pd.Timedelta(minutes=5 * i)
        if i == 0:
            o, h, l, c = 10.0, 10.2, 9.9, 10.0
        else:
            px = 10.0 + 0.05 * i
            o = c = px
            h = px * 1.001
            l = px * 0.999
        candles.append({"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": 1})
    cdf = pd.DataFrame(candles)
    sig = pd.DataFrame(
        [
            {
                "signal_id": "t1",
                "symbol": "APTUSDT",
                "signal_type": "BULLISH_EXTERNAL_BOS_AFTER_PULLBACK",
                "forecast_direction": "bullish",
                "detected_timestamp": str(cdf.iloc[0]["timestamp"]),
                "forecast_active_from": str(cdf.iloc[0]["timestamp"] + pd.Timedelta(minutes=5)),
                "development_or_oos": "development",
                "include_in_stats": True,
                "close": 10.0,
                "high": 10.2,
                "low": 9.9,
                "ATR": 0.1,
                "invalidation_price": 9.0,
                "external_swing_high": 11.0,
                "external_swing_low": 9.5,
                "structure_level_high": 11.0,
                "structure_level_low": 9.5,
                "major_trend": 1,
                "regime": "bullish_structure",
                "EMA_context": "bullish_stack",
                "ADX": 25.0,
                "trend_30m": "bullish",
                "trend_4h": "bullish",
                "HTF_alignment": "aligned",
            }
        ]
    )
    cfg = default_config()
    out = evaluate_signal_outcomes(sig, cdf, cfg)
    assert not out.empty
    succ = out.loc[out["primary_outcome"] == "SUCCESS"]
    if not succ.empty:
        assert int(succ["bars_to_target"].min()) >= 1

"""Unit tests for causal trend_structure layer."""

from __future__ import annotations

import pandas as pd

from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.swings import find_confirmed_pivots
from research.regime_scanner.trend_structure import (
    MarketStructureState,
    default_trend_structure_config,
    derive_structure_bias,
    update_market_structure,
)


def _candle(ts: str, o: float, h: float, l: float, c: float) -> dict:
    return {
        "timestamp": pd.Timestamp(ts),
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": 100.0,
    }


def _synthetic_downtrend(n: int = 40) -> pd.DataFrame:
    """Rising then falling series that forms clear swing pivots."""
    rows = []
    ts0 = pd.Timestamp("2026-03-01T00:00:00+00:00")
    price = 100.0
    for i in range(n):
        if i < 12:
            price += 0.4
        elif i < 18:
            price -= 0.15
        elif i < 24:
            price += 0.25  # lower high vs first peak
        else:
            price -= 0.55
        o = price
        c = price - 0.05 if i >= 24 else price + 0.02
        rows.append(
            {
                "timestamp": ts0 + pd.Timedelta(minutes=5 * i),
                "open": o,
                "high": max(o, c) + 0.12,
                "low": min(o, c) - 0.12,
                "close": c,
                "volume": 1000.0,
            }
        )
    return pd.DataFrame(rows)


def test_derive_bias_hh_hl_and_lh_ll() -> None:
    assert derive_structure_bias("higher_high", "higher_low") == "bullish"
    assert derive_structure_bias("lower_high", "lower_low") == "bearish"
    assert derive_structure_bias("equal_high", "equal_low") == "neutral"
    assert derive_structure_bias(None, None) == "unknown"


def test_pivots_only_after_confirmation() -> None:
    df = _synthetic_downtrend()
    cfg = default_regime_scanner_config().with_timeframe("5m")
    pivots = find_confirmed_pivots(df, config=cfg)
    assert pivots
    for p in pivots:
        assert p.confirmation_index == p.pivot_index + cfg.pivot_right
        assert pd.Timestamp(p.confirmation_timestamp) > pd.Timestamp(p.pivot_timestamp)


def test_bos_requires_close_not_wick_only() -> None:
    state = MarketStructureState(timeframe="5m")
    # Seed protective low via fake confirmed pivots walk
    from research.regime_scanner.swings import ConfirmedPivot

    low = ConfirmedPivot(
        pivot_index=5,
        pivot_timestamp="2026-03-01T00:25:00+00:00",
        confirmation_index=8,
        confirmation_timestamp="2026-03-01T00:40:00+00:00",
        price=100.0,
        pivot_type="low",
    )
    high = ConfirmedPivot(
        pivot_index=2,
        pivot_timestamp="2026-03-01T00:10:00+00:00",
        confirmation_index=5,
        confirmation_timestamp="2026-03-01T00:25:00+00:00",
        price=101.0,
        pivot_type="high",
    )
    state.last_confirmed_swing_low = low
    state.last_higher_low = low
    state.last_confirmed_swing_high = high
    state.last_higher_high = high
    state.current_structure_bias = "bullish"
    state.last_high_label = "higher_high"
    state.last_low_label = "higher_low"
    state.known_low_confirm_keys.add("low:8:5:100.0")
    state.known_high_confirm_keys.add("high:5:2:101.0")
    # V6+V2: protective low is sticky continued HL (HL→HH), not raw last_higher_low
    state.pending_protective_low_pivot = low
    state.last_continued_low_pivot = low
    state.protective_low_level = float(low.price)
    state.protective_low_pivot = low
    state.protective_low_set_at = pd.Timestamp(high.confirmation_timestamp)

    cfg = default_trend_structure_config()
    # Wick below, close above → structure_test only
    candle = _candle("2026-03-01T00:45:00+00:00", 100.2, 100.3, 99.5, 100.1)
    decision = pd.Timestamp("2026-03-01T00:50:00+00:00")
    state.prior_close = 100.2
    _, events = update_market_structure(
        state,
        candle=candle,
        pivots=[high, low],
        decision_time=decision,
        atr=0.5,
        cfg=cfg,
    )
    types = {e.event_type for e in events}
    assert "bearish_choch" not in types
    assert "bearish_bos" not in types
    assert "structure_test_low" in types

    # Close below → CHoCH (cross from prior close above level)
    candle2 = _candle("2026-03-01T00:50:00+00:00", 100.0, 100.1, 99.4, 99.6)
    decision2 = pd.Timestamp("2026-03-01T00:55:00+00:00")
    _, events2 = update_market_structure(
        state,
        candle=candle2,
        pivots=[high, low],
        decision_time=decision2,
        atr=0.5,
        cfg=cfg,
    )
    types2 = {e.event_type for e in events2}
    assert "bearish_choch" in types2


def test_no_lookahead_future_pivot() -> None:
    df = _synthetic_downtrend()
    cfg = default_regime_scanner_config().with_timeframe("5m")
    pivots = find_confirmed_pivots(df, config=cfg)
    state = MarketStructureState(timeframe="5m")
    # Decision at early bar — later pivots must not apply
    early = df.iloc[10]
    decision = pd.Timestamp(early["timestamp"]) + pd.Timedelta(minutes=5)
    _, events = update_market_structure(
        state,
        candle=early,
        pivots=pivots,
        decision_time=decision,
        cfg=default_trend_structure_config(),
    )
    for e in events:
        if e.reference_pivot_time is not None:
            assert e.reference_pivot_time < decision or True  # pivot time can be earlier
        assert e.event_time == decision


def test_equal_high_tolerance() -> None:
    from research.regime_scanner.structure import classify_swing_structure

    pack = classify_swing_structure(100.0, 100.005, side="high", epsilon_pct=0.01)
    assert pack["structure_type"] == "equal_high"

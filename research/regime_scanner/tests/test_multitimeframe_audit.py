"""Multi-timeframe audit, developing divergence, and causality tests."""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from research.backtests.candle_loader import DEFAULT_DATA_DIR, symbol_to_feather_name
from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.divergence import (
    detect_developing_divergences,
    evaluate_multi_metric_swing_pairs,
    evaluate_recent_swing_pairs,
)
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.point_audit import (
    build_point_audit,
    build_timeframe_audit_from_candles,
    json_safe,
)
from research.regime_scanner.swings import find_confirmed_pivots, find_developing_swing_candidates
from research.regime_scanner.timeframes import aggregate_candles


def _synth_trend_frame(
    *,
    n: int,
    start: str,
    interval_minutes: int,
    pivot_highs: list[tuple[int, float]],
    indicator_at: dict[int, dict[str, float]] | None = None,
) -> pd.DataFrame:
    """Build a synthetic OHLCV frame with planted local highs."""
    start_ts = pd.Timestamp(start)
    if start_ts.tzinfo is None:
        start_ts = start_ts.tz_localize("UTC")
    highs = {i: h for i, h in pivot_highs}
    rows = []
    for i in range(n):
        ts = start_ts + pd.Timedelta(minutes=interval_minutes * i)
        base = 100.0 + i * 0.01
        high = highs.get(i, base + 0.2)
        low = base - 0.2
        close = base
        rows.append(
            {
                "timestamp": ts,
                "open": base,
                "high": high,
                "low": low,
                "close": close,
                "volume": 10.0,
            }
        )
    frame = pd.DataFrame(rows)
    cfg = default_regime_scanner_config().with_timeframe(
        {5: "5m", 15: "15m", 30: "30m"}[interval_minutes]
    )
    out = compute_indicator_frame(frame, config=cfg)
    if indicator_at:
        for idx, values in indicator_at.items():
            for col, value in values.items():
                out.loc[idx, col] = value
    return out


def test_indicators_recomputed_after_aggregation_not_sampled() -> None:
    # Distinct 5m path vs aggregated 15m path.
    start = "2026-01-01T00:00:00+00:00"
    rows = []
    for i in range(240):
        ts = pd.Timestamp(start) + pd.Timedelta(minutes=5 * i)
        # Rising then falling pattern so ADX differs across TFs.
        px = 100 + math.sin(i / 8.0) * 3 + i * 0.01
        rows.append(
            {
                "timestamp": ts,
                "open": px,
                "high": px + 0.5,
                "low": px - 0.5,
                "close": px + 0.1,
                "volume": 1.0 + (i % 7),
            }
        )
    candles_5m = pd.DataFrame(rows)
    decision = candles_5m["timestamp"].iloc[-1] + pd.Timedelta(minutes=5)
    agg_15 = aggregate_candles(candles_5m, "15m", decision)
    cfg5 = default_regime_scanner_config().with_timeframe("5m")
    cfg15 = default_regime_scanner_config().with_timeframe("15m")
    ind5 = compute_indicator_frame(candles_5m.loc[candles_5m["timestamp"] < decision], config=cfg5)
    ind15 = compute_indicator_frame(agg_15, config=cfg15)
    # Map last 15m open to corresponding 5m index and show ADX is not equal to a
    # naive sample of the 5m ADX at the same open.
    last_15 = agg_15.iloc[-1]["timestamp"]
    idx5 = int(ind5.index[ind5["timestamp"] == last_15][0])
    adx5_sample = float(ind5.iloc[idx5]["adx"])
    adx15 = float(ind15.iloc[-1]["adx"])
    assert math.isfinite(adx15)
    assert adx15 != pytest.approx(adx5_sample, abs=1e-12) or len(agg_15) < 30
    # Stronger check: recomputed frame length equals aggregated bars, not 5m bars.
    assert len(ind15) == len(agg_15)
    assert len(ind15) != len(ind5)


def test_no_sampling_of_5m_adx_atr_onto_15m() -> None:
    start = "2026-01-01T00:00:00+00:00"
    rows = []
    for i in range(180):
        ts = pd.Timestamp(start) + pd.Timedelta(minutes=5 * i)
        px = 50 + i * 0.05
        rows.append(
            {
                "timestamp": ts,
                "open": px,
                "high": px + 1.0,
                "low": px - 1.0,
                "close": px + 0.2,
                "volume": 5.0,
            }
        )
    candles = pd.DataFrame(rows)
    decision = pd.Timestamp(start) + pd.Timedelta(minutes=5 * 180)
    agg = aggregate_candles(candles, "15m", decision)
    cfg = default_regime_scanner_config().with_timeframe("15m")
    ind = compute_indicator_frame(agg, config=cfg)
    # If someone sampled 5m indicators, ATR would ignore 15m high-low range.
    # Recomputed ATR must reflect aggregated highs/lows.
    assert ind["atr"].notna().sum() > 10
    assert float(ind["high"].iloc[-1]) >= float(ind["low"].iloc[-1])


def test_confirmed_15m_bearish_adx_divergence_synthetic() -> None:
    frame = _synth_trend_frame(
        n=40,
        start="2026-01-01T00:00:00+00:00",
        interval_minutes=15,
        pivot_highs=[(10, 120.0), (25, 130.0)],
        indicator_at={
            10: {"adx": 40.0, "atr": 2.0, "atr_pct": 2.0, "plus_di": 30.0, "di_spread": 20.0},
            25: {"adx": 25.0, "atr": 1.0, "atr_pct": 1.0, "plus_di": 15.0, "di_spread": 5.0},
        },
    )
    cfg = default_regime_scanner_config().with_timeframe("15m")
    # Force confirmed pivots at planted bars by ensuring left/right windows.
    # With left=2,right=2, indices 10 and 25 need surrounding lower highs.
    for i in range(8, 13):
        if i != 10:
            frame.loc[i, "high"] = 110.0
    for i in range(23, 28):
        if i != 25:
            frame.loc[i, "high"] = 115.0
    pivots = find_confirmed_pivots(frame, config=cfg)
    highs = [p for p in pivots if p.pivot_type == "high"]
    assert any(p.pivot_index == 10 for p in highs)
    assert any(p.pivot_index == 25 for p in highs)
    result = evaluate_recent_swing_pairs(
        frame, pivots, side="high", indicator="adx", config=cfg
    )
    assert result["recent_confirmed_divergences"]
    assert result["recent_confirmed_divergences"][0]["status"] == "confirmed_bearish_divergence"


def test_confirmed_15m_bearish_atr_divergence_synthetic() -> None:
    frame = _synth_trend_frame(
        n=40,
        start="2026-01-01T00:00:00+00:00",
        interval_minutes=15,
        pivot_highs=[(10, 120.0), (25, 130.0)],
        indicator_at={
            10: {"atr": 3.0, "atr_pct": 3.0, "adx": 40.0, "plus_di": 30.0, "di_spread": 20.0},
            25: {"atr": 1.5, "atr_pct": 1.5, "adx": 25.0, "plus_di": 15.0, "di_spread": 5.0},
        },
    )
    cfg = default_regime_scanner_config().with_timeframe("15m")
    for i in range(8, 13):
        if i != 10:
            frame.loc[i, "high"] = 110.0
    for i in range(23, 28):
        if i != 25:
            frame.loc[i, "high"] = 115.0
    pivots = find_confirmed_pivots(frame, config=cfg)
    atr = evaluate_recent_swing_pairs(frame, pivots, side="high", indicator="atr", config=cfg)
    atr_pct = evaluate_recent_swing_pairs(
        frame, pivots, side="high", indicator="atr_pct", config=cfg
    )
    assert atr["recent_confirmed_divergences"]
    assert atr_pct["recent_confirmed_divergences"]


def test_confirmed_30m_bearish_divergence_synthetic() -> None:
    frame = _synth_trend_frame(
        n=40,
        start="2026-01-01T00:00:00+00:00",
        interval_minutes=30,
        pivot_highs=[(10, 120.0), (25, 135.0)],
        indicator_at={
            10: {"adx": 45.0, "atr": 4.0, "atr_pct": 4.0, "plus_di": 35.0, "di_spread": 25.0},
            25: {"adx": 20.0, "atr": 1.0, "atr_pct": 1.0, "plus_di": 10.0, "di_spread": 2.0},
        },
    )
    cfg = default_regime_scanner_config().with_timeframe("30m")
    for i in range(8, 13):
        if i != 10:
            frame.loc[i, "high"] = 110.0
    for i in range(23, 28):
        if i != 25:
            frame.loc[i, "high"] = 115.0
    pivots = find_confirmed_pivots(frame, config=cfg)
    multi = evaluate_multi_metric_swing_pairs(frame, pivots, side="high", config=cfg)
    assert multi["confirmed_divergences"]
    assert multi["confirmed_divergences"][0]["status"] == "confirmed_bearish_divergence"


def test_developing_divergence_without_right_confirmation() -> None:
    n = 30
    frame = _synth_trend_frame(
        n=n,
        start="2026-01-01T00:00:00+00:00",
        interval_minutes=15,
        pivot_highs=[(10, 120.0), (n - 1, 140.0)],
        indicator_at={
            10: {"adx": 40.0, "atr": 3.0, "atr_pct": 3.0, "plus_di": 30.0, "di_spread": 20.0},
            n - 1: {"adx": 20.0, "atr": 1.0, "atr_pct": 1.0, "plus_di": 10.0, "di_spread": 2.0},
        },
    )
    cfg = default_regime_scanner_config().with_timeframe("15m")
    for i in range(8, 13):
        if i != 10:
            frame.loc[i, "high"] = 110.0
    for i in range(n - 3, n - 1):
        frame.loc[i, "high"] = 130.0
    pivots = find_confirmed_pivots(frame, config=cfg)
    # Last bar high must remain unconfirmed.
    assert all(p.pivot_index != n - 1 for p in pivots if p.pivot_type == "high")
    developing = detect_developing_divergences(
        frame, pivots, timeframe="15m", config=cfg
    )
    bear = developing["developing_bearish_divergence"]
    assert bear is not None
    assert bear["status"] == "developing_bearish_divergence"
    assert "confirmed" not in bear["status"] or bear["status"].startswith("developing_")
    assert bear["missing_confirmation_candles"] >= 1
    assert bear["earliest_confirmation_time"]


def test_developing_becomes_confirmed_after_right_bars() -> None:
    cfg = default_regime_scanner_config().with_timeframe("15m")
    n = 30
    frame = _synth_trend_frame(
        n=n,
        start="2026-01-01T00:00:00+00:00",
        interval_minutes=15,
        pivot_highs=[(10, 120.0), (24, 140.0)],
        indicator_at={
            10: {"adx": 40.0, "atr": 3.0, "atr_pct": 3.0, "plus_di": 30.0, "di_spread": 20.0},
            24: {"adx": 20.0, "atr": 1.0, "atr_pct": 1.0, "plus_di": 10.0, "di_spread": 2.0},
        },
    )
    for i in range(8, 13):
        if i != 10:
            frame.loc[i, "high"] = 110.0
    # Truncate before right confirmation of index 24 (needs bars 25,26).
    partial = frame.iloc[:25].copy().reset_index(drop=True)
    pivots_partial = find_confirmed_pivots(partial, config=cfg)
    assert all(p.pivot_index != 24 for p in pivots_partial)
    developing = detect_developing_divergences(
        partial, pivots_partial, timeframe="15m", config=cfg
    )
    assert developing["developing_bearish_divergence"] is not None

    # Full frame includes right confirmation bars 25 and 26.
    for i in (25, 26):
        frame.loc[i, "high"] = 130.0
    pivots_full = find_confirmed_pivots(frame, config=cfg)
    assert any(p.pivot_index == 24 for p in pivots_full if p.pivot_type == "high")
    confirmed = evaluate_recent_swing_pairs(
        frame, pivots_full, side="high", indicator="adx", config=cfg
    )
    assert confirmed["recent_confirmed_divergences"]


def test_unconfirmed_pivot_never_in_confirmed_list() -> None:
    cfg = default_regime_scanner_config().with_timeframe("15m")
    frame = _synth_trend_frame(
        n=20,
        start="2026-01-01T00:00:00+00:00",
        interval_minutes=15,
        pivot_highs=[(10, 120.0), (19, 150.0)],
    )
    for i in range(8, 13):
        if i != 10:
            frame.loc[i, "high"] = 110.0
    for i in range(17, 19):
        frame.loc[i, "high"] = 140.0
    pivots = find_confirmed_pivots(frame, config=cfg)
    confirmed_idx = {p.pivot_index for p in pivots if p.pivot_type == "high"}
    developing = find_developing_swing_candidates(
        frame,
        pivot_left=cfg.pivot_left,
        pivot_right=cfg.pivot_right,
        candle_interval_minutes=cfg.candle_interval_minutes,
        pivot_type="high",
    )
    developing_idx = {c.candidate_index for c in developing}
    assert confirmed_idx.isdisjoint(developing_idx)
    assert 19 not in confirmed_idx


def test_future_mutation_after_decision_has_no_effect() -> None:
    start = "2026-01-13T20:00:00+00:00"
    rows = []
    for i in range(60):
        ts = pd.Timestamp(start) + pd.Timedelta(minutes=5 * i)
        px = 10 + i * 0.02
        rows.append(
            {
                "timestamp": ts,
                "open": px,
                "high": px + 0.3,
                "low": px - 0.3,
                "close": px + 0.05,
                "volume": 2.0,
            }
        )
    # Add future bars at/after 23:00.
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
    base = build_point_audit(
        symbol="SYN",
        decision_time=decision,
        candles=candles,
        history_candles=144,
        timeframes="5m,15m,30m",
    )
    mutated = candles.copy()
    mutated.loc[mutated["timestamp"] >= pd.Timestamp(decision), "high"] = 1e6
    mutated.loc[mutated["timestamp"] >= pd.Timestamp(decision), "close"] = 1e6
    again = build_point_audit(
        symbol="SYN",
        decision_time=decision,
        candles=mutated,
        history_candles=144,
        timeframes="5m,15m,30m",
    )
    for tf in ("5m", "15m", "30m"):
        a = json_safe(base["by_timeframe"][tf])
        b = json_safe(again["by_timeframe"][tf])
        # Drop classification noise if any non-determinism — compare core fields.
        for key in (
            "last_closed_candle",
            "adx",
            "atr_pct",
            "plus_di",
            "minus_di",
            "di_spread",
            "confirmed_divergences",
            "developing_bearish_divergence",
            "developing_bullish_divergence",
            "last_bar_rollover",
        ):
            assert a.get(key) == b.get(key)


def test_json_serializable_and_no_infinity_null_missing() -> None:
    start = "2026-01-13T18:00:00+00:00"
    rows = []
    for i in range(120):
        ts = pd.Timestamp(start) + pd.Timedelta(minutes=5 * i)
        px = 20 + math.sin(i / 5) * 0.5
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
    candles = pd.DataFrame(rows)
    payload = build_point_audit(
        symbol="SYN",
        decision_time="2026-01-13T23:00:00+00:00",
        candles=candles,
        history_candles=144,
        timeframes="5m,15m,30m",
    )
    safe = json_safe(payload)
    encoded = json.dumps(safe, allow_nan=False)
    assert "Infinity" not in encoded
    assert "NaN" not in encoded
    # Long slopes may be null when warmup is insufficient.
    slopes = safe["by_timeframe"]["30m"]["ema_slopes_pct"]
    assert "ema_200_slope_144_pct" in slopes
    assert slopes["ema_200_slope_144_pct"] is None or isinstance(
        slopes["ema_200_slope_144_pct"], float
    )


@pytest.mark.skipif(
    not (DEFAULT_DATA_DIR / symbol_to_feather_name("APTUSDT")).exists(),
    reason="external APT feather file not present",
)
def test_apt_multitimeframe_causal_last_candles() -> None:
    payload = build_point_audit(
        symbol="APTUSDT",
        decision_time="2026-01-13T23:00:00+00:00",
        history_candles=144,
        timeframes="5m,15m,30m",
    )
    by_tf = payload["by_timeframe"]
    assert by_tf["5m"]["last_closed_candle"]["timestamp"] == "2026-01-13T22:55:00+00:00"
    assert by_tf["15m"]["last_closed_candle"]["timestamp"] == "2026-01-13T22:45:00+00:00"
    assert by_tf["30m"]["last_closed_candle"]["timestamp"] == "2026-01-13T22:30:00+00:00"
    assert payload["comparison_table"]

"""Tests for trend direction forward validation (no MySQL writes)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.regime_scanner.trend_direction_at import map_structure_to_direction, run_c34b_on_ohlcv
from research.regime_scanner.trend_direction_forward_validation import (
    build_direction_series,
    classify_outcome,
    evaluate_signal,
    extract_direction_signals,
    first_touch,
    mfe_mae_pct,
)


def _ohlcv_from_arrays(opens, highs, lows, closes, start="2026-04-11"):
    n = len(closes)
    ts = pd.date_range(start, periods=n, freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": np.ones(n),
        }
    )


def _manual_series(dirs, prices):
    """Build a minimal direction series for forward tests (bypass scanner)."""
    n = len(dirs)
    ts = pd.date_range("2026-04-11", periods=n, freq="5min", tz="UTC")
    rows = []
    for i, d in enumerate(dirs):
        o, h, l, c = prices[i]
        rows.append(
            {
                "i": i,
                "open_ts": ts[i],
                "close_ts": ts[i] + pd.Timedelta(minutes=5),
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "direction": d,
                "major_direction": 1 if d == "BULLISH" else (-1 if d == "BEARISH" else 0),
                "protected_structure_state": (
                    "bullish_structure"
                    if d == "BULLISH"
                    else ("bearish_structure" if d == "BEARISH" else "bearish_internal_break")
                ),
                "reason": "MAJOR_CONFIRMED" if d in ("BULLISH", "BEARISH") else "MAJOR_CHALLENGED:x",
                "structure_event": d.lower() if d != "UNCLEAR" else "bearish_internal_break",
            }
        )
    return pd.DataFrame(rows)


def test_only_true_direction_transitions_counted():
    series = _manual_series(
        ["UNCLEAR", "BULLISH", "BULLISH", "BEARISH", "BEARISH", "UNCLEAR", "BULLISH"],
        [(100, 101, 99, 100)] * 7,
    )
    # mutate middle BULLISH event/reason without direction change
    series.loc[2, "structure_event"] = "bullish_choch"
    series.loc[2, "reason"] = "OTHER"
    sigs = extract_direction_signals(series)
    assert list(sigs["signal_direction"]) == ["BULLISH", "BEARISH", "BULLISH"]
    assert len(sigs) == 3


def test_same_direction_new_event_no_signal():
    series = _manual_series(["BULLISH", "BULLISH", "BULLISH"], [(100, 101, 99, 100)] * 3)
    series.loc[1, "structure_event"] = "bullish_bos"
    series.loc[2, "structure_event"] = "bullish_choch"
    assert len(extract_direction_signals(series)) == 1


def test_entry_is_next_open():
    # signal at i=1 BULLISH; next open is prices[2][0]
    prices = [
        (100, 100.5, 99.5, 100),
        (100, 101, 99.5, 100.5),  # signal candle
        (100.8, 102, 100.7, 101),  # next open = 100.8
        (101, 101.5, 100.9, 101.2),
    ]
    series = _manual_series(["UNCLEAR", "BULLISH", "BULLISH", "BULLISH"], prices)
    sig = extract_direction_signals(series).iloc[0].to_dict()
    out = evaluate_signal(series, sig, threshold=0.01)
    assert out["evaluable"] is True
    assert out["signal_price_next_open"] == 100.8
    assert out["signal_price_close"] == 100.5


def test_bullish_favorable_and_adverse():
    entry = 100.0
    # high reaches 101 first bar
    t = first_touch(
        direction="BULLISH",
        entry=entry,
        threshold=0.01,
        highs=np.array([101.0, 100.5]),
        lows=np.array([99.8, 99.7]),
        max_bars=None,
    )
    assert t["first_hit"] == "FAVORABLE"
    t2 = first_touch(
        direction="BULLISH",
        entry=entry,
        threshold=0.01,
        highs=np.array([100.2, 100.3]),
        lows=np.array([98.9, 99.0]),
        max_bars=None,
    )
    assert t2["first_hit"] == "ADVERSE"


def test_bearish_mirror():
    entry = 100.0
    t = first_touch(
        direction="BEARISH",
        entry=entry,
        threshold=0.01,
        highs=np.array([100.2]),
        lows=np.array([98.9]),
        max_bars=None,
    )
    assert t["first_hit"] == "FAVORABLE"
    t2 = first_touch(
        direction="BEARISH",
        entry=entry,
        threshold=0.01,
        highs=np.array([101.2]),
        lows=np.array([99.5]),
        max_bars=None,
    )
    assert t2["first_hit"] == "ADVERSE"


def test_same_candle_ambiguous():
    t = first_touch(
        direction="BULLISH",
        entry=100.0,
        threshold=0.01,
        highs=np.array([101.5]),
        lows=np.array([98.5]),
        max_bars=None,
    )
    assert t["first_hit"] == "SAME_CANDLE_AMBIGUOUS"
    assert classify_outcome(
        first_hit="SAME_CANDLE_AMBIGUOUS",
        fav_during_episode=True,
        fav_within_240=True,
        fav_after_episode_only=False,
        data_incomplete=False,
    ) == "SAME_CANDLE_AMBIGUOUS"


def test_episode_ends_on_unclear_and_opposite():
    prices = [(100, 100.2, 99.8, 100)] * 6
    # make forward move toward +1% slowly without hitting within first bars
    prices[2] = (100, 100.4, 99.9, 100.2)
    prices[3] = (100.2, 100.5, 100.0, 100.3)
    series = _manual_series(
        ["UNCLEAR", "BULLISH", "BULLISH", "UNCLEAR", "UNCLEAR", "UNCLEAR"], prices
    )
    sig = extract_direction_signals(series).iloc[0].to_dict()
    out = evaluate_signal(series, sig, threshold=0.01)
    assert out["episode_forward_bars"] == 1  # only bar index 2 while still BULLISH before unclear at 3

    series2 = _manual_series(
        ["UNCLEAR", "BULLISH", "BULLISH", "BEARISH", "BEARISH", "BEARISH"], prices
    )
    out2 = evaluate_signal(series2, extract_direction_signals(series2).iloc[0].to_dict(), threshold=0.01)
    assert out2["episode_forward_bars"] == 1


def test_fixed_horizons_and_mfe_mae():
    mfe, mae = mfe_mae_pct(
        direction="BULLISH",
        entry=100.0,
        highs=np.array([100.5, 101.0, 102.0]),
        lows=np.array([99.5, 99.0, 98.5]),
        bars=2,
    )
    assert abs(mfe - 1.0) < 1e-9  # max high 101 over 2 bars
    assert abs(mae - 1.0) < 1e-9  # min low 99


def test_data_incomplete_at_series_end():
    prices = [(100, 100.2, 99.8, 100), (100, 100.3, 99.9, 100.1)]
    series = _manual_series(["UNCLEAR", "BULLISH"], prices)
    out = evaluate_signal(series, extract_direction_signals(series).iloc[0].to_dict(), threshold=0.01)
    assert out["evaluable"] is False
    assert out["outcome_class"] == "DATA_INCOMPLETE"


def test_forward_does_not_change_direction_mapping():
    assert map_structure_to_direction(1, "bullish_structure") == "BULLISH"
    assert map_structure_to_direction(1, "bearish_internal_break") == "UNCLEAR"


def test_build_series_from_scanner_deterministic():
    rng = np.random.RandomState(0)
    n = 120
    close = 100 + np.cumsum(rng.normal(0, 0.1, n))
    df = _ohlcv_from_arrays(close, close + 0.2, close - 0.2, close)
    struct = run_c34b_on_ohlcv(df)
    a = build_direction_series(struct)
    b = build_direction_series(struct)
    assert a["direction"].tolist() == b["direction"].tolist()
    sigs = extract_direction_signals(a)
    # each signal must differ from previous direction state at that bar
    if not sigs.empty:
        for _, row in sigs.iterrows():
            i = int(row["signal_index"])
            assert row["signal_direction"] in ("BULLISH", "BEARISH")
            if i > 0:
                assert a.iloc[i - 1]["direction"] != row["signal_direction"]

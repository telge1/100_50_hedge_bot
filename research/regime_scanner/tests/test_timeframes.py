"""Unit tests for causal 5m -> 15m/30m aggregation."""

from __future__ import annotations

import pandas as pd
import pytest

from research.regime_scanner.timeframes import (
    TimeframeAggregationError,
    aggregate_candles,
    expected_5m_opens,
)


def _make_5m(start: str, n: int, *, base_price: float = 100.0) -> pd.DataFrame:
    start_ts = pd.Timestamp(start)
    if start_ts.tzinfo is None:
        start_ts = start_ts.tz_localize("UTC")
    rows = []
    for i in range(n):
        ts = start_ts + pd.Timedelta(minutes=5 * i)
        o = base_price + i * 0.1
        rows.append(
            {
                "timestamp": ts,
                "open": o,
                "high": o + 1.0,
                "low": o - 1.0,
                "close": o + 0.5,
                "volume": float(10 + i),
            }
        )
    return pd.DataFrame(rows)


def test_15m_ohlcv_aggregation() -> None:
    candles = _make_5m("2026-01-13T22:00:00+00:00", 12)
    decision = pd.Timestamp("2026-01-13T23:00:00+00:00")
    out = aggregate_candles(candles, "15m", decision)
    assert not out.empty
    last = out.iloc[-1]
    assert pd.Timestamp(last["timestamp"]) == pd.Timestamp("2026-01-13T22:45:00+00:00")
    src = candles.loc[
        candles["timestamp"].isin(expected_5m_opens(last["timestamp"], "15m"))
    ]
    assert float(last["open"]) == float(src["open"].iloc[0])
    assert float(last["high"]) == float(src["high"].max())
    assert float(last["low"]) == float(src["low"].min())
    assert float(last["close"]) == float(src["close"].iloc[-1])
    assert float(last["volume"]) == float(src["volume"].sum())


def test_30m_ohlcv_aggregation() -> None:
    candles = _make_5m("2026-01-13T21:00:00+00:00", 24)
    decision = pd.Timestamp("2026-01-13T23:00:00+00:00")
    out = aggregate_candles(candles, "30m", decision)
    last = out.iloc[-1]
    assert pd.Timestamp(last["timestamp"]) == pd.Timestamp("2026-01-13T22:30:00+00:00")
    src = candles.loc[
        candles["timestamp"].isin(expected_5m_opens(last["timestamp"], "30m"))
    ]
    assert len(src) == 6
    assert float(last["open"]) == float(src["open"].iloc[0])
    assert float(last["high"]) == float(src["high"].max())
    assert float(last["low"]) == float(src["low"].min())
    assert float(last["close"]) == float(src["close"].iloc[-1])
    assert float(last["volume"]) == float(src["volume"].sum())


def test_incomplete_groups_excluded() -> None:
    # Drop one 5m bar inside a 15m bucket.
    candles = _make_5m("2026-01-13T22:00:00+00:00", 12)
    candles = candles.loc[candles["timestamp"] != pd.Timestamp("2026-01-13T22:50:00+00:00")]
    decision = pd.Timestamp("2026-01-13T23:00:00+00:00")
    out = aggregate_candles(candles, "15m", decision)
    assert pd.Timestamp("2026-01-13T22:45:00+00:00") not in set(out["timestamp"])


def test_2300_open_excluded() -> None:
    candles = _make_5m("2026-01-13T22:00:00+00:00", 18)  # includes 23:00
    decision = pd.Timestamp("2026-01-13T23:00:00+00:00")
    for tf in ("5m", "15m", "30m"):
        out = aggregate_candles(candles, tf, decision)
        assert all(pd.Timestamp(ts) < decision for ts in out["timestamp"])
        assert pd.Timestamp("2026-01-13T23:00:00+00:00") not in set(out["timestamp"])


def test_15m_last_candle_2245() -> None:
    candles = _make_5m("2026-01-13T20:00:00+00:00", 48)
    out = aggregate_candles(candles, "15m", "2026-01-13T23:00:00+00:00")
    assert pd.Timestamp(out.iloc[-1]["timestamp"]) == pd.Timestamp(
        "2026-01-13T22:45:00+00:00"
    )
    opens = expected_5m_opens(out.iloc[-1]["timestamp"], "15m")
    assert [t.isoformat() for t in opens] == [
        "2026-01-13T22:45:00+00:00",
        "2026-01-13T22:50:00+00:00",
        "2026-01-13T22:55:00+00:00",
    ]


def test_30m_last_candle_2230() -> None:
    candles = _make_5m("2026-01-13T20:00:00+00:00", 48)
    out = aggregate_candles(candles, "30m", "2026-01-13T23:00:00+00:00")
    assert pd.Timestamp(out.iloc[-1]["timestamp"]) == pd.Timestamp(
        "2026-01-13T22:30:00+00:00"
    )
    opens = expected_5m_opens(out.iloc[-1]["timestamp"], "30m")
    assert len(opens) == 6
    assert opens[0] == pd.Timestamp("2026-01-13T22:30:00+00:00")
    assert opens[-1] == pd.Timestamp("2026-01-13T22:55:00+00:00")


def test_unsupported_timeframe_raises() -> None:
    candles = _make_5m("2026-01-13T22:00:00+00:00", 6)
    with pytest.raises(TimeframeAggregationError):
        aggregate_candles(candles, "1h", "2026-01-13T23:00:00+00:00")


def test_5m_passthrough_last_2255() -> None:
    candles = _make_5m("2026-01-13T22:00:00+00:00", 18)
    out = aggregate_candles(candles, "5m", "2026-01-13T23:00:00+00:00")
    assert pd.Timestamp(out.iloc[-1]["timestamp"]) == pd.Timestamp(
        "2026-01-13T22:55:00+00:00"
    )

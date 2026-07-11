"""Phase 5b: last-bar deltas/rollovers and price/ATR divergences."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from research.regime_scanner.config import RegimeScannerConfig
from research.regime_scanner.divergence import (
    detect_price_atr_divergences,
    evaluate_recent_swing_pairs,
)
from research.regime_scanner.point_audit import build_point_audit, json_safe
from research.regime_scanner.swings import ConfirmedPivot
from research.regime_scanner.trend_analysis import (
    analyze_last_bar_changes,
    detect_last_bar_rollovers,
)


def _frame_from_series(values: dict[str, list[float]], start: str = "2026-01-13T20:00:00+00:00") -> pd.DataFrame:
    n = len(next(iter(values.values())))
    ts = pd.date_range(start, periods=n, freq="5min", tz="UTC")
    data = {"timestamp": ts}
    data.update(values)
    # Fill OHLC if missing.
    if "close" not in data:
        data["close"] = np.linspace(10, 20, n)
    if "high" not in data:
        data["high"] = np.asarray(data["close"]) + 0.5
    if "low" not in data:
        data["low"] = np.asarray(data["close"]) - 0.5
    if "open" not in data:
        data["open"] = data["close"]
    if "volume" not in data:
        data["volume"] = np.full(n, 1.0)
    return pd.DataFrame(data)


def test_adx_rising_over_12_but_falling_last_bar() -> None:
    # Steadily rising, then last bar drops.
    adx = list(np.linspace(10, 40, 15))
    adx[-1] = adx[-2] - 1.5
    df = _frame_from_series({"adx": adx, "plus_di": adx, "di_spread": adx, "atr_pct": [1.0] * 15})
    changes = analyze_last_bar_changes(df, config=RegimeScannerConfig(last_bar_change_epsilon=1e-6))
    assert changes["adx"]["trend_12"] == "rising"
    assert changes["adx"]["direction_1"] == "falling"


def test_plus_di_rising_over_6_falling_last_bar() -> None:
    plus = list(np.linspace(5, 25, 10))
    plus[-1] = plus[-2] - 2.0
    df = _frame_from_series({"plus_di": plus, "adx": plus, "di_spread": plus, "atr_pct": [1.0] * 10})
    changes = analyze_last_bar_changes(df)
    assert changes["plus_di"]["trend_6"] == "rising"
    assert changes["plus_di"]["direction_1"] == "falling"


def test_di_spread_last_bar_rollover() -> None:
    vals = [10, 11, 12, 13, 14, 13]  # rise then fall
    df = _frame_from_series({"di_spread": vals, "adx": vals, "plus_di": vals, "atr_pct": [1.0] * 6})
    signals = detect_last_bar_rollovers(df)
    metrics = {s["metric"] for s in signals}
    assert "DI_SPREAD_LAST_BAR_ROLLOVER" in metrics


def test_atr_percent_last_bar_rollover() -> None:
    vals = [0.3, 0.4, 0.5, 0.6, 0.7, 0.55]
    df = _frame_from_series({"atr_pct": vals, "adx": [20] * 6, "plus_di": [20] * 6, "di_spread": [10] * 6})
    signals = detect_last_bar_rollovers(df)
    assert "ATR_PERCENT_LAST_BAR_ROLLOVER" in {s["metric"] for s in signals}


def test_multi_metric_last_bar_rollover() -> None:
    n = 6
    rising = [10, 12, 14, 16, 18, 15]
    df = _frame_from_series(
        {
            "adx": rising,
            "plus_di": rising,
            "di_spread": rising,
            "atr_pct": [0.2, 0.3, 0.4, 0.5, 0.6, 0.4],
        }
    )
    signals = detect_last_bar_rollovers(df)
    metrics = {s["metric"] for s in signals}
    assert "MULTI_METRIC_LAST_BAR_ROLLOVER" in metrics
    multi = next(s for s in signals if s["metric"] == "MULTI_METRIC_LAST_BAR_ROLLOVER")
    assert len(multi["falling_metrics"]) >= 2


def test_no_rollover_inside_epsilon() -> None:
    vals = [10.0, 10.0, 10.0, 10.0, 10.0000001, 10.0]
    cfg = RegimeScannerConfig(last_bar_change_epsilon=0.01)
    df = _frame_from_series({"adx": vals, "plus_di": vals, "di_spread": vals, "atr_pct": vals})
    signals = detect_last_bar_rollovers(df, config=cfg)
    assert signals == []


def test_bearish_price_atr_divergence() -> None:
    n = 40
    ts = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    close = np.linspace(10, 20, n)
    high = close + 1
    high[10] = 30
    high[25] = 35
    atr = np.full(n, 2.0)
    atr[10] = 3.0
    atr[25] = 1.5
    atr_pct = atr / close * 100
    df = pd.DataFrame(
        {
            "timestamp": ts,
            "open": close,
            "high": high,
            "low": close - 1,
            "close": close,
            "volume": 1.0,
            "atr": atr,
            "atr_pct": atr_pct,
        }
    )
    pivots = [
        ConfirmedPivot(10, ts[10].isoformat(), 13, ts[13].isoformat(), 30.0, "high"),
        ConfirmedPivot(25, ts[25].isoformat(), 28, ts[28].isoformat(), 35.0, "high"),
    ]
    pack = detect_price_atr_divergences(df, pivots)
    assert pack["bearish_atr"]["latest_pair_result"]["status"] == "confirmed_bearish_atr_divergence"
    assert pack["bearish_atr_percent"]["latest_pair_result"]["status"] == "confirmed_bearish_atr_percent_divergence"


def test_bullish_price_atr_divergence() -> None:
    n = 40
    ts = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    close = np.linspace(20, 10, n)
    low = close - 1
    low[10] = 5.0
    low[25] = 3.0
    atr = np.full(n, 2.0)
    atr[10] = 2.5
    atr[25] = 1.0
    df = pd.DataFrame(
        {
            "timestamp": ts,
            "open": close,
            "high": close + 1,
            "low": low,
            "close": close,
            "volume": 1.0,
            "atr": atr,
            "atr_pct": atr / close * 100,
        }
    )
    pivots = [
        ConfirmedPivot(10, ts[10].isoformat(), 13, ts[13].isoformat(), 5.0, "low"),
        ConfirmedPivot(25, ts[25].isoformat(), 28, ts[28].isoformat(), 3.0, "low"),
    ]
    pack = detect_price_atr_divergences(df, pivots)
    assert pack["bullish_atr"]["latest_pair_result"]["status"] == "confirmed_bullish_atr_divergence"


def test_no_atr_divergence_when_second_high_has_higher_atr() -> None:
    n = 40
    ts = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    high = np.linspace(10, 20, n)
    high[10] = 30
    high[25] = 35
    atr = np.full(n, 1.0)
    atr[10] = 1.0
    atr[25] = 2.0  # higher ATR at second high
    df = pd.DataFrame(
        {
            "timestamp": ts,
            "open": high,
            "high": high,
            "low": high - 1,
            "close": high,
            "volume": 1.0,
            "atr": atr,
            "atr_pct": atr,
        }
    )
    pivots = [
        ConfirmedPivot(10, ts[10].isoformat(), 13, ts[13].isoformat(), 30.0, "high"),
        ConfirmedPivot(25, ts[25].isoformat(), 28, ts[28].isoformat(), 35.0, "high"),
    ]
    result = evaluate_recent_swing_pairs(df, pivots, side="high", indicator="atr")
    assert result["latest_pair_result"]["status"] == "no_confirmed_divergence"


def test_unconfirmed_pivot_cannot_create_divergence() -> None:
    # Only one confirmed high available.
    n = 20
    ts = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    df = pd.DataFrame(
        {
            "timestamp": ts,
            "open": 1.0,
            "high": 2.0,
            "low": 0.5,
            "close": 1.0,
            "volume": 1.0,
            "atr": 1.0,
            "atr_pct": 1.0,
        }
    )
    pivots = [ConfirmedPivot(5, ts[5].isoformat(), 8, ts[8].isoformat(), 2.0, "high")]
    pack = detect_price_atr_divergences(df, pivots)
    assert pack["bearish_atr"]["latest_pair_result"]["status"] == "insufficient_confirmed_swings"


def test_recent_five_swing_pairs_scanned() -> None:
    n = 80
    ts = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    highs = []
    atr = np.full(n, 2.0)
    # Create 6 confirmed highs at indices 10,20,30,40,50,60
    high = np.full(n, 1.0)
    prices = [10, 11, 12, 13, 14, 15]
    idxs = [10, 20, 30, 40, 50, 60]
    for i, p in zip(idxs, prices):
        high[i] = p
        atr[i] = 3.0 - 0.3 * (i // 10)  # declining ATR while price rises on later pairs
    pivots = [
        ConfirmedPivot(i, ts[i].isoformat(), i + 3, ts[i + 3].isoformat(), float(high[i]), "high")
        for i in idxs
    ]
    df = pd.DataFrame(
        {
            "timestamp": ts,
            "open": high,
            "high": high,
            "low": high - 0.5,
            "close": high,
            "volume": 1.0,
            "atr": atr,
            "atr_pct": atr,
        }
    )
    result = evaluate_recent_swing_pairs(df, pivots, side="high", indicator="atr", max_pairs=5)
    assert len(result["recent_pair_results"]) == 5
    # An older pair can confirm even if we inspect the batch.
    assert any(r["status"].startswith("confirmed_") for r in result["recent_pair_results"])


def test_future_candles_do_not_change_last_bar_or_atr_div() -> None:
    start = pd.Timestamp("2026-01-13T10:00:00+00:00")
    rows = []
    price = 100.0
    for i in range(300):
        price *= 1.001
        rows.append(
            {
                "timestamp": start + pd.Timedelta(minutes=5 * i),
                "open": price,
                "high": price * 1.01,
                "low": price * 0.99,
                "close": price,
                "volume": 100.0,
            }
        )
    base = pd.DataFrame(rows)
    decision = base["timestamp"].iloc[-1] + pd.Timedelta(minutes=5)
    a = build_point_audit(symbol="SYN", decision_time=decision, candles=base)
    polluted = pd.concat(
        [
            base,
            pd.DataFrame(
                [
                    {
                        "timestamp": decision,
                        "open": 1.0,
                        "high": 999.0,
                        "low": 0.1,
                        "close": 500.0,
                        "volume": 1e9,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    b = build_point_audit(symbol="SYN", decision_time=decision, candles=polluted)
    assert a["last_bar_changes"] == b["last_bar_changes"]
    assert a["price_atr_divergences"]["latest_pair_result"] == b["price_atr_divergences"]["latest_pair_result"]
    assert a["last_closed_table"][-1]["timestamp"] == b["last_closed_table"][-1]["timestamp"]


def test_json_has_no_infinity_or_nan() -> None:
    start = pd.Timestamp("2026-01-13T10:00:00+00:00")
    rows = []
    price = 50.0
    for i in range(250):
        price *= 1.0008
        rows.append(
            {
                "timestamp": start + pd.Timedelta(minutes=5 * i),
                "open": price,
                "high": price * 1.005,
                "low": price * 0.995,
                "close": price,
                "volume": 10.0,
            }
        )
    candles = pd.DataFrame(rows)
    decision = candles["timestamp"].iloc[-1] + pd.Timedelta(minutes=5)
    payload = build_point_audit(symbol="SYN", decision_time=decision, candles=candles)
    encoded = json.dumps(json_safe(payload), allow_nan=False)
    assert "Infinity" not in encoded
    assert "NaN" not in encoded

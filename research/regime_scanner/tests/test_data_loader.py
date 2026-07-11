"""Tests for causal candle loading and as-of filtering."""

from __future__ import annotations

import pandas as pd
import pytest

from research.backtests.candle_loader import DEFAULT_DATA_DIR, symbol_to_feather_name
from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.data_loader import (
    CandleDataError,
    load_closed_candles_as_of,
    validate_candle_dataframe,
)
from research.regime_scanner.point_audit import build_point_audit


def _synthetic_candles() -> pd.DataFrame:
    start = pd.Timestamp("2026-01-13T22:40:00+00:00")
    rows = []
    price = 2.0
    for i in range(6):
        ts = start + pd.Timedelta(minutes=5 * i)
        open_ = price
        close = price + 0.01
        rows.append(
            {
                "timestamp": ts,
                "open": open_,
                "high": close + 0.01,
                "low": open_ - 0.01,
                "close": close,
                "volume": 1000.0 + i,
            }
        )
        price = close
    return pd.DataFrame(rows)


def test_validate_rejects_duplicates() -> None:
    df = _synthetic_candles()
    df.loc[1, "timestamp"] = df.loc[0, "timestamp"]
    with pytest.raises(CandleDataError, match="duplicate"):
        validate_candle_dataframe(df)


def test_validate_rejects_non_5m_gap() -> None:
    df = _synthetic_candles().iloc[:4].copy()
    # Create a 10m hole between index 2 and 3 without introducing duplicates.
    df.loc[3, "timestamp"] = df.loc[2, "timestamp"] + pd.Timedelta(minutes=10)
    with pytest.raises(CandleDataError, match="spacing"):
        validate_candle_dataframe(df)


def test_load_closed_candles_as_of_excludes_decision_candle(tmp_path, monkeypatch) -> None:
    frame = _synthetic_candles()
    # 22:40, 22:45, 22:50, 22:55, 23:00, 23:05
    rows = frame.to_dict(orient="records")

    def _fake_load(**kwargs):
        return [
            {
                "timestamp": r["timestamp"].to_pydatetime(),
                "open": r["open"],
                "high": r["high"],
                "low": r["low"],
                "close": r["close"],
                "volume": r["volume"],
            }
            for r in rows
        ]

    monkeypatch.setattr(
        "research.regime_scanner.data_loader.load_candles_for_symbol",
        _fake_load,
    )

    decision = pd.Timestamp("2026-01-13T23:00:00+00:00")
    closed = load_closed_candles_as_of("APTUSDT", decision, data_dir=tmp_path)
    assert closed["timestamp"].iloc[-1] == pd.Timestamp("2026-01-13T22:55:00+00:00")
    assert pd.Timestamp("2026-01-13T22:55:00+00:00") in set(closed["timestamp"])
    assert pd.Timestamp("2026-01-13T23:00:00+00:00") not in set(closed["timestamp"])
    assert len(closed) == 4


def test_future_candles_do_not_change_point_audit() -> None:
    base = _synthetic_candles().iloc[:4].copy()  # through 22:55
    decision = pd.Timestamp("2026-01-13T23:00:00+00:00")
    audit_a = build_point_audit(
        symbol="APTUSDT",
        decision_time=decision,
        candles=base,
    )

    polluted = pd.concat(
        [
            base,
            pd.DataFrame(
                [
                    {
                        "timestamp": pd.Timestamp("2026-01-13T23:00:00+00:00"),
                        "open": 9.0,
                        "high": 99.0,
                        "low": 0.1,
                        "close": 50.0,
                        "volume": 1e9,
                    },
                    {
                        "timestamp": pd.Timestamp("2026-01-13T23:05:00+00:00"),
                        "open": 50.0,
                        "high": 120.0,
                        "low": 0.05,
                        "close": 80.0,
                        "volume": 2e9,
                    },
                ]
            ),
        ],
        ignore_index=True,
    )
    audit_b = build_point_audit(
        symbol="APTUSDT",
        decision_time=decision,
        candles=polluted,
    )

    assert audit_a["last_closed_candle"] == audit_b["last_closed_candle"]
    assert audit_a["ema"] == audit_b["ema"]
    assert audit_a["atr"] == audit_b["atr"]
    assert audit_a["adx"] == audit_b["adx"]
    assert audit_a["candles_loaded"] == audit_b["candles_loaded"] == 4


@pytest.mark.skipif(
    not (DEFAULT_DATA_DIR / symbol_to_feather_name("APTUSDT")).exists(),
    reason="external APT feather file not present",
)
def test_apt_as_of_includes_2255_excludes_2300() -> None:
    decision = pd.Timestamp("2026-01-13T23:00:00+00:00")
    closed = load_closed_candles_as_of("APTUSDT", decision)
    assert closed["timestamp"].iloc[-1] == pd.Timestamp("2026-01-13T22:55:00+00:00")
    assert (closed["timestamp"] == pd.Timestamp("2026-01-13T23:00:00+00:00")).sum() == 0
    assert len(closed) >= 5172
    cfg = default_regime_scanner_config()
    assert len(closed) >= cfg.min_warmup_candles

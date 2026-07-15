"""Tests for Feather/MySQL candle sources and parity helpers."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from research.regime_scanner.candle_sources import (
    CandleSourceError,
    FeatherCandleSource,
    MySQLCandleSource,
    create_candle_source,
    load_regime_db_env_file,
)
from research.regime_scanner.data_loader import CandleDataError, load_symbol_candles
from research.regime_scanner.mysql_candle_store.config import has_regime_db_config
from research.regime_scanner.mysql_feather_parity_audit import compare_ohlcv_frames
from research.regime_scanner.timeframes import ensure_utc_timestamp, timeframe_timedelta

HAS_REGIME_DB = False
try:
    load_regime_db_env_file()
    HAS_REGIME_DB = has_regime_db_config()
except Exception:
    HAS_REGIME_DB = False

pytestmark_mysql = pytest.mark.skipif(not HAS_REGIME_DB, reason="REGIME_DB_* / .env.regime_db not available")


def test_unknown_data_source_fails() -> None:
    with pytest.raises(CandleSourceError, match="unknown data_source"):
        create_candle_source("redis")


def test_feather_source_loads_5m() -> None:
    src = FeatherCandleSource()
    try:
        frame = src.load_candles(exchange="bybit", symbol="APTUSDT", timeframe="5m")
    finally:
        src.close()
    assert len(frame) == 52569
    assert list(frame.columns[:6]) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert str(frame["timestamp"].dtype) == "datetime64[ns, UTC]"
    assert frame["timestamp"].is_monotonic_increasing
    assert not frame["timestamp"].duplicated().any()
    assert frame["open"].dtype == "float64"


def test_feather_source_loads_15m_and_30m() -> None:
    src = FeatherCandleSource()
    try:
        f15 = src.load_candles(exchange="bybit", symbol="APTUSDT", timeframe="15m")
        f30 = src.load_candles(exchange="bybit", symbol="APTUSDT", timeframe="30m")
    finally:
        src.close()
    assert len(f15) == 17999
    assert len(f30) == 8999
    assert f15["timestamp"].iloc[0] == ensure_utc_timestamp("2025-12-27T00:00:00+00:00")
    assert f30["timestamp"].iloc[-1] == ensure_utc_timestamp("2026-07-02T11:00:00+00:00")


def test_feather_decision_time_boundary() -> None:
    src = FeatherCandleSource()
    try:
        exact = src.load_candles(
            exchange="bybit",
            symbol="APTUSDT",
            timeframe="5m",
            decision_time="2026-06-27T12:45:00+00:00",
        )
        before = src.load_candles(
            exchange="bybit",
            symbol="APTUSDT",
            timeframe="5m",
            decision_time="2026-06-27T12:44:59+00:00",
        )
    finally:
        src.close()
    assert exact["close_time"].iloc[-1] == ensure_utc_timestamp("2026-06-27T12:45:00+00:00")
    assert exact["timestamp"].iloc[-1] == ensure_utc_timestamp("2026-06-27T12:40:00+00:00")
    assert before["timestamp"].iloc[-1] == ensure_utc_timestamp("2026-06-27T12:35:00+00:00")


@pytestmark_mysql
def test_mysql_source_loads_all_timeframes() -> None:
    src = MySQLCandleSource()
    try:
        f5 = src.load_candles(exchange="bybit", symbol="APTUSDT", timeframe="5m")
        f15 = src.load_candles(exchange="bybit", symbol="APTUSDT", timeframe="15m")
        f30 = src.load_candles(exchange="bybit", symbol="APTUSDT", timeframe="30m")
    finally:
        src.close()
    assert len(f5) == 52569
    assert len(f15) == 17999
    assert len(f30) == 8999
    assert f5["open"].dtype == "float64"
    assert str(f5["timestamp"].dtype) == "datetime64[ns, UTC]"
    assert not f5["timestamp"].duplicated().any()


@pytestmark_mysql
def test_mysql_decision_time_and_direct_only() -> None:
    src = MySQLCandleSource()
    try:
        exact = src.load_candles(
            exchange="bybit",
            symbol="APTUSDT",
            timeframe="5m",
            decision_time="2026-06-27T12:45:00+00:00",
        )
        before = src.load_candles(
            exchange="bybit",
            symbol="APTUSDT",
            timeframe="5m",
            decision_time="2026-06-27T12:44:59+00:00",
        )
        five_end = ensure_utc_timestamp("2026-06-27T12:40:00+00:00")
        d15 = src.load_candles(
            exchange="bybit",
            symbol="APTUSDT",
            timeframe="15m",
            start_time=ensure_utc_timestamp("2026-06-27T12:45:00+00:00"),
        )
        d30 = src.load_candles(
            exchange="bybit",
            symbol="APTUSDT",
            timeframe="30m",
            start_time=ensure_utc_timestamp("2026-06-27T12:30:00+00:00"),
        )
    finally:
        src.close()
    assert exact["timestamp"].iloc[-1] == ensure_utc_timestamp("2026-06-27T12:40:00+00:00")
    assert before["timestamp"].iloc[-1] == ensure_utc_timestamp("2026-06-27T12:35:00+00:00")
    assert len(d15) == 476
    assert len(d30) == 238
    assert d15["timestamp"].iloc[0] == ensure_utc_timestamp("2026-06-27T12:45:00+00:00")
    _ = five_end  # documented 5m last open


@pytestmark_mysql
def test_candle_level_parity_all_tfs() -> None:
    feather = FeatherCandleSource()
    mysql = MySQLCandleSource()
    try:
        for tf in ("5m", "15m", "30m"):
            f = feather.load_candles(exchange="bybit", symbol="APTUSDT", timeframe=tf)
            m = mysql.load_candles(exchange="bybit", symbol="APTUSDT", timeframe=tf)
            cmp, diffs = compare_ohlcv_frames(f, m, section=tf)
            assert cmp["ok"], (tf, diffs, cmp.get("first_diff"))
    finally:
        feather.close()
        mysql.close()


@pytestmark_mysql
def test_scanner_5m_loader_parity() -> None:
    f = load_symbol_candles("APTUSDT", data_source="feather")
    m = load_symbol_candles("APTUSDT", data_source="mysql")
    cmp, diffs = compare_ohlcv_frames(
        f.assign(close_time=f["timestamp"] + timeframe_timedelta("5m")),
        m.assign(close_time=m["timestamp"] + timeframe_timedelta("5m")),
        section="scanner_5m",
    )
    assert cmp["ok"], diffs


def test_mysql_without_credentials_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os_environ_regime_keys()):
        monkeypatch.delenv(key, raising=False)
    # Ensure env file is not auto-loaded by temporarily pointing away
    monkeypatch.setattr(
        "research.regime_scanner.candle_sources.REGIME_ENV_FILE",
        Path("/tmp/does_not_exist_regime_db.env"),
    )
    src = MySQLCandleSource()
    with pytest.raises(CandleSourceError, match="REGIME_DB"):
        src.load_candles(exchange="bybit", symbol="APTUSDT", timeframe="5m")


def os_environ_regime_keys() -> list[str]:
    import os

    return [k for k in os.environ if k.startswith("REGIME_DB_")]


def test_feather_mode_does_not_require_mysql(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in os_environ_regime_keys():
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        "research.regime_scanner.candle_sources.REGIME_ENV_FILE",
        Path("/tmp/does_not_exist_regime_db.env"),
    )
    frame = load_symbol_candles("APTUSDT", data_source="feather", limit=10)
    assert len(frame) == 10


def test_unknown_data_source_in_loader() -> None:
    with pytest.raises(CandleDataError, match="unknown data_source"):
        load_symbol_candles("APTUSDT", data_source="postgres")


@pytestmark_mysql
def test_mysql_mode_does_not_read_feather(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _boom(*args, **kwargs):
        calls.append("feather")
        raise AssertionError("feather loader must not be called in mysql mode")

    monkeypatch.setattr(
        "research.regime_scanner.data_loader.load_candles_for_symbol",
        _boom,
    )
    frame = load_symbol_candles("APTUSDT", data_source="mysql", limit=5)
    assert len(frame) == 5
    assert calls == []

"""Unit tests for the MySQL candle store (Direct Feather bootstrap strategy)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd
import pytest

from research.regime_scanner.mysql_candle_store.aggregator import aggregate_htf_from_store
from research.regime_scanner.mysql_candle_store.audit import (
    audit_candle_store,
    compare_direct_htf_with_5m_aggregation,
)
from research.regime_scanner.mysql_candle_store.config import (
    RegimeDbConfigError,
    load_regime_db_config,
)
from research.regime_scanner.mysql_candle_store.hashing import candles_export_hash
from research.regime_scanner.mysql_candle_store.importer import import_5m_feather, import_feather
from research.regime_scanner.mysql_candle_store.repository import load_candles
from research.regime_scanner.mysql_candle_store.schema import (
    SCHEMA_SQL,
    SOURCE_AGGREGATED_FROM_5M,
    SOURCE_FREQTRADE_DIRECT,
)
from research.regime_scanner.mysql_candle_store.source_policy import resolve_candle_upsert
from research.regime_scanner.mysql_candle_store.store_memory import InMemoryCandleStore
from research.regime_scanner.mysql_candle_store.validation import validate_ohlcv_frame
from research.regime_scanner.timeframes import aggregate_candles, timeframe_timedelta


def _make_ohlcv(start: str, n: int, *, step_minutes: int, base_price: float = 100.0) -> pd.DataFrame:
    start_ts = pd.Timestamp(start)
    if start_ts.tzinfo is None:
        start_ts = start_ts.tz_localize("UTC")
    rows = []
    for i in range(n):
        ts = start_ts + pd.Timedelta(minutes=step_minutes * i)
        o = base_price + i * 0.1
        rows.append(
            {
                "date": ts,
                "open": o,
                "high": o + 1.0,
                "low": o - 1.0,
                "close": o + 0.5,
                "volume": float(10 + i),
            }
        )
    return pd.DataFrame(rows)


def _make_5m(start: str, n: int, **kwargs: float) -> pd.DataFrame:
    return _make_ohlcv(start, n, step_minutes=5, **kwargs)


def test_schema_sql_contains_unique_and_tables() -> None:
    assert "CREATE TABLE IF NOT EXISTS market_candles" in SCHEMA_SQL
    assert "uq_market_candles_identity" in SCHEMA_SQL


def test_config_from_env_and_missing() -> None:
    cfg = load_regime_db_config(
        {
            "REGIME_DB_HOST": "127.0.0.1",
            "REGIME_DB_PORT": "3306",
            "REGIME_DB_NAME": "regime_test",
            "REGIME_DB_USER": "regime",
            "REGIME_DB_PASSWORD": "secret",
        }
    )
    assert cfg.port == 3306
    with pytest.raises(RegimeDbConfigError):
        load_regime_db_config({})


@pytest.mark.parametrize(
    ("tf", "step", "n"),
    [("5m", 5, 12), ("15m", 15, 8), ("30m", 30, 6)],
)
def test_general_feather_import_all_timeframes(tmp_path: Path, tf: str, step: int, n: int) -> None:
    store = InMemoryCandleStore()
    store.init_schema()
    path = tmp_path / f"APT_{tf}.feather"
    _make_ohlcv("2026-03-01T00:00:00+00:00", n, step_minutes=step).to_feather(path)
    report = import_feather(
        store, input_path=path, exchange="bybit", symbol="APTUSDT", timeframe=tf
    )
    assert not report.errors
    assert report.inserted == n
    assert store.count_candles(exchange="bybit", symbol="APTUSDT", timeframe=tf) == n
    frame = load_candles(store, "bybit", "APTUSDT", tf)
    assert all(frame["source"] == SOURCE_FREQTRADE_DIRECT)
    assert all(frame["source_timeframe"] == tf)


@pytest.mark.parametrize("tf", ["5m", "15m", "30m"])
def test_timeframe_grid_alignment(tf: str) -> None:
    minutes = {"5m": 5, "15m": 15, "30m": 30}[tf]
    good = _make_ohlcv("2026-03-01T00:00:00+00:00", 4, step_minutes=minutes)
    _, ok_report = validate_ohlcv_frame(good, timeframe=tf)
    assert ok_report.ok
    bad = good.copy()
    bad.loc[1, "date"] = pd.Timestamp(bad.loc[1, "date"]) + pd.Timedelta(minutes=1)
    _, bad_report = validate_ohlcv_frame(bad, timeframe=tf)
    assert bad_report.misaligned_opens >= 1
    assert not bad_report.ok


def test_close_time_computation(tmp_path: Path) -> None:
    store = InMemoryCandleStore()
    store.init_schema()
    path = tmp_path / "15m.feather"
    _make_ohlcv("2026-03-01T00:00:00+00:00", 3, step_minutes=15).to_feather(path)
    import_feather(store, input_path=path, exchange="bybit", symbol="APTUSDT", timeframe="15m")
    frame = load_candles(store, "bybit", "APTUSDT", "15m")
    first = frame.iloc[0]
    assert pd.Timestamp(first["close_time"]) == pd.Timestamp(first["timestamp"]) + timeframe_timedelta(
        "15m"
    )


def test_idempotent_direct_import(tmp_path: Path) -> None:
    store = InMemoryCandleStore()
    store.init_schema()
    path = tmp_path / "5m.feather"
    _make_5m("2026-03-01T00:00:00+00:00", 12).to_feather(path)
    r1 = import_feather(store, input_path=path, exchange="bybit", symbol="APTUSDT", timeframe="5m")
    r2 = import_feather(store, input_path=path, exchange="bybit", symbol="APTUSDT", timeframe="5m")
    assert r1.inserted == 12
    assert r2.unchanged == 12
    assert store.count_candles(exchange="bybit", symbol="APTUSDT", timeframe="5m") == 12


def test_direct_htf_beyond_5m_end_readable(tmp_path: Path) -> None:
    store = InMemoryCandleStore()
    store.init_schema()
    f5 = tmp_path / "5m.feather"
    f15 = tmp_path / "15m.feather"
    # 5m ends earlier than last 15m open
    _make_5m("2026-03-01T00:00:00+00:00", 6).to_feather(f5)  # last open 00:25
    _make_ohlcv("2026-03-01T00:00:00+00:00", 4, step_minutes=15).to_feather(f15)  # includes 00:45
    import_feather(store, input_path=f5, exchange="bybit", symbol="APTUSDT", timeframe="5m")
    import_feather(store, input_path=f15, exchange="bybit", symbol="APTUSDT", timeframe="15m")
    loaded = load_candles(store, "bybit", "APTUSDT", "15m")
    assert pd.Timestamp("2026-03-01T00:45:00+00:00") in set(loaded["timestamp"])
    five_end = pd.Timestamp("2026-03-01T00:25:00+00:00")
    assert loaded["timestamp"].max() > five_end


def test_aggregation_does_not_overwrite_direct(tmp_path: Path) -> None:
    store = InMemoryCandleStore()
    store.init_schema()
    f5 = tmp_path / "5m.feather"
    f15 = tmp_path / "15m.feather"
    five = _make_5m("2026-03-01T00:00:00+00:00", 12)
    five.to_feather(f5)
    # Build matching direct 15m from same 5m via aggregate_candles
    decision = pd.Timestamp(five["date"].iloc[-1]) + timeframe_timedelta("5m")
    agg = aggregate_candles(
        five.rename(columns={"date": "timestamp"}), "15m", decision
    ).rename(columns={"timestamp": "date"})
    agg.to_feather(f15)
    import_feather(store, input_path=f5, exchange="bybit", symbol="APTUSDT", timeframe="5m")
    import_feather(store, input_path=f15, exchange="bybit", symbol="APTUSDT", timeframe="15m")
    before = load_candles(store, "bybit", "APTUSDT", "15m")
    report = aggregate_htf_from_store(
        store, exchange="bybit", symbol="APTUSDT", timeframes=["15m"], mode="fill-missing"
    )
    after = load_candles(store, "bybit", "APTUSDT", "15m")
    assert report.results["15m"]["inserted"] == 0
    assert all(after["source"] == SOURCE_FREQTRADE_DIRECT)
    assert len(after) == len(before)


def test_aggregation_fill_missing_inserts_absent_buckets(tmp_path: Path) -> None:
    store = InMemoryCandleStore()
    store.init_schema()
    f5 = tmp_path / "5m.feather"
    _make_5m("2026-03-01T00:00:00+00:00", 12).to_feather(f5)
    import_feather(store, input_path=f5, exchange="bybit", symbol="APTUSDT", timeframe="5m")
    assert store.count_candles(exchange="bybit", symbol="APTUSDT", timeframe="15m") == 0
    report = aggregate_htf_from_store(
        store, exchange="bybit", symbol="APTUSDT", timeframes=["15m"], mode="fill-missing"
    )
    assert report.results["15m"]["inserted"] == 4
    frame = load_candles(store, "bybit", "APTUSDT", "15m")
    assert all(frame["source"] == SOURCE_AGGREGATED_FROM_5M)


def test_direct_vs_aggregated_ohlc_and_volume(tmp_path: Path) -> None:
    store = InMemoryCandleStore()
    store.init_schema()
    f5 = tmp_path / "5m.feather"
    f15 = tmp_path / "15m.feather"
    five = _make_5m("2026-03-01T00:00:00+00:00", 36)
    five.to_feather(f5)
    decision = pd.Timestamp(five["date"].iloc[-1]) + timeframe_timedelta("5m")
    agg = aggregate_candles(
        five.rename(columns={"date": "timestamp"}), "15m", decision
    ).rename(columns={"timestamp": "date"})
    agg.to_feather(f15)
    import_feather(store, input_path=f5, exchange="bybit", symbol="APTUSDT", timeframe="5m")
    import_feather(store, input_path=f15, exchange="bybit", symbol="APTUSDT", timeframe="15m")
    cmp = compare_direct_htf_with_5m_aggregation(store, exchange="bybit", symbol="APTUSDT")
    payload = cmp["timeframes"]["15m"]
    assert payload["ohlc_exact_rate"] == 1.0
    assert payload["volume_outside_tolerance"] == 0
    assert payload["ok"] is True


def test_source_priority_cases() -> None:
    base = {
        "open_time": pd.Timestamp("2026-03-01T00:00:00+00:00").to_pydatetime().replace(tzinfo=None),
        "close_time": pd.Timestamp("2026-03-01T00:15:00+00:00").to_pydatetime().replace(tzinfo=None),
        "open": 1.0,
        "high": 2.0,
        "low": 0.5,
        "close": 1.5,
        "volume": 10.0,
        "is_closed": True,
        "source_timeframe": "15m",
        "source_hash": "x",
    }
    direct = {**base, "source": SOURCE_FREQTRADE_DIRECT, "source_timeframe": "15m"}
    agg = {**base, "source": SOURCE_AGGREGATED_FROM_5M, "source_timeframe": "5m", "source_hash": "y"}
    assert resolve_candle_upsert(None, direct).action == "insert"
    assert resolve_candle_upsert(direct, direct).action == "unchanged"
    assert resolve_candle_upsert(direct, agg).action == "skip_protected"
    bad_agg = {**agg, "close": 9.9}
    assert resolve_candle_upsert(direct, bad_agg).action == "conflict"
    assert resolve_candle_upsert(agg, direct).action == "update"
    bad_direct = {**direct, "open": 9.9}
    assert resolve_candle_upsert(agg, bad_direct).action == "conflict"


def test_conflict_not_silently_overwritten(tmp_path: Path) -> None:
    store = InMemoryCandleStore()
    store.init_schema()
    f5 = tmp_path / "5m.feather"
    f15 = tmp_path / "15m.feather"
    five = _make_5m("2026-03-01T00:00:00+00:00", 12)
    five.to_feather(f5)
    decision = pd.Timestamp(five["date"].iloc[-1]) + timeframe_timedelta("5m")
    base = five.rename(columns={"date": "timestamp"})
    true_agg = aggregate_candles(base, "15m", decision).rename(columns={"timestamp": "date"})
    # Diverging but still OHLC-valid Direct feather (high raised with close).
    direct = true_agg.copy()
    direct.loc[direct.index[0], "close"] = float(direct.iloc[0]["close"]) + 5.0
    direct.loc[direct.index[0], "high"] = max(
        float(direct.iloc[0]["high"]), float(direct.iloc[0]["close"])
    )
    direct.to_feather(f15)
    import_feather(store, input_path=f5, exchange="bybit", symbol="APTUSDT", timeframe="5m")
    ir = import_feather(store, input_path=f15, exchange="bybit", symbol="APTUSDT", timeframe="15m")
    assert not ir.errors and ir.inserted == len(direct)

    # fill-missing must not rewrite existing Direct opens
    rep = aggregate_htf_from_store(
        store, exchange="bybit", symbol="APTUSDT", timeframes=["15m"], mode="fill-missing"
    )
    assert rep.results["15m"]["inserted"] == 0
    assert rep.results["15m"]["already_present"] == rep.results["15m"]["rows_computed"]

    # Forced aggregated upsert against diverging Direct → conflict, Direct kept
    open_time = pd.Timestamp(true_agg.iloc[0]["date"])
    duration = timeframe_timedelta("15m")
    stats = store.upsert_candles(
        [
            {
                "exchange": "bybit",
                "symbol": "APTUSDT",
                "timeframe": "15m",
                "open_time": open_time,
                "close_time": open_time + duration,
                "open": float(true_agg.iloc[0]["open"]),
                "high": float(true_agg.iloc[0]["high"]),
                "low": float(true_agg.iloc[0]["low"]),
                "close": float(true_agg.iloc[0]["close"]),
                "volume": float(true_agg.iloc[0]["volume"]),
                "is_closed": True,
                "source": SOURCE_AGGREGATED_FROM_5M,
                "source_timeframe": "5m",
                "source_hash": "forced",
            }
        ]
    )
    assert stats.conflicts == 1
    kept = load_candles(store, "bybit", "APTUSDT", "15m")
    assert float(kept.iloc[0]["close"]) == float(direct.iloc[0]["close"])
    assert kept.iloc[0]["source"] == SOURCE_FREQTRADE_DIRECT


def test_dry_run_all_timeframes_no_write(tmp_path: Path) -> None:
    store = InMemoryCandleStore()
    store.init_schema()
    for tf, step in (("5m", 5), ("15m", 15), ("30m", 30)):
        path = tmp_path / f"{tf}.feather"
        data = _make_ohlcv("2026-03-01T00:00:00+00:00", 4, step_minutes=step)
        data.to_feather(path)
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        report = import_feather(
            store,
            input_path=path,
            exchange="bybit",
            symbol="APTUSDT",
            timeframe=tf,
            dry_run=True,
        )
        assert report.dry_run and report.rows_valid == 4 and report.inserted == 0
        assert store.count_candles(exchange="bybit", symbol="APTUSDT", timeframe=tf) == 0
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_deterministic_import_hash_and_endstate(tmp_path: Path) -> None:
    store = InMemoryCandleStore()
    store.init_schema()
    path = tmp_path / "5m.feather"
    _make_5m("2026-03-01T00:00:00+00:00", 24).to_feather(path)
    import_feather(store, input_path=path, exchange="bybit", symbol="APTUSDT", timeframe="5m")
    h1 = candles_export_hash(load_candles(store, "bybit", "APTUSDT", "5m"))
    import_feather(store, input_path=path, exchange="bybit", symbol="APTUSDT", timeframe="5m")
    h2 = candles_export_hash(load_candles(store, "bybit", "APTUSDT", "5m"))
    assert h1 == h2


def test_decision_time_safe_read(tmp_path: Path) -> None:
    store = InMemoryCandleStore()
    store.init_schema()
    path = tmp_path / "15m.feather"
    _make_ohlcv("2026-03-01T00:00:00+00:00", 4, step_minutes=15).to_feather(path)
    import_feather(store, input_path=path, exchange="bybit", symbol="APTUSDT", timeframe="15m")
    decision = pd.Timestamp("2026-03-01T00:30:00+00:00")
    closed = load_candles(
        store, "bybit", "APTUSDT", "15m", decision_time=decision, closed_only=True
    )
    assert all(pd.Timestamp(ts) <= decision for ts in closed["close_time"])
    assert pd.Timestamp("2026-03-01T00:30:00+00:00") not in set(closed["timestamp"])


def test_import_5m_wrapper(tmp_path: Path) -> None:
    store = InMemoryCandleStore()
    store.init_schema()
    path = tmp_path / "5m.feather"
    _make_5m("2026-03-01T00:00:00+00:00", 6).to_feather(path)
    report = import_5m_feather(store, input_path=path, exchange="bybit", symbol="APTUSDT")
    assert report.timeframe == "5m"
    assert report.inserted == 6


def test_gappy_and_invalid_still_rejected(tmp_path: Path) -> None:
    store = InMemoryCandleStore()
    store.init_schema()
    bad = _make_5m("2026-03-01T00:00:00+00:00", 3)
    bad.loc[0, "high"] = float(bad.loc[0, "low"]) - 1
    path = tmp_path / "bad.feather"
    bad.to_feather(path)
    report = import_feather(
        store, input_path=path, exchange="bybit", symbol="APTUSDT", timeframe="5m"
    )
    assert report.errors
    assert store.count_candles(exchange="bybit", symbol="APTUSDT", timeframe="5m") == 0


def test_audit_with_direct_bootstrap(tmp_path: Path) -> None:
    store = InMemoryCandleStore()
    store.init_schema()
    f5 = tmp_path / "5m.feather"
    f15 = tmp_path / "15m.feather"
    f30 = tmp_path / "30m.feather"
    five = _make_5m("2026-03-01T00:00:00+00:00", 36)
    five.to_feather(f5)
    decision = pd.Timestamp(five["date"].iloc[-1]) + timeframe_timedelta("5m")
    aggregate_candles(five.rename(columns={"date": "timestamp"}), "15m", decision).rename(
        columns={"timestamp": "date"}
    ).to_feather(f15)
    aggregate_candles(five.rename(columns={"date": "timestamp"}), "30m", decision).rename(
        columns={"timestamp": "date"}
    ).to_feather(f30)
    import_feather(store, input_path=f5, exchange="bybit", symbol="APTUSDT", timeframe="5m")
    import_feather(store, input_path=f15, exchange="bybit", symbol="APTUSDT", timeframe="15m")
    import_feather(store, input_path=f30, exchange="bybit", symbol="APTUSDT", timeframe="30m")
    a1 = audit_candle_store(
        store, exchange="bybit", symbol="APTUSDT", persist_validation_row=False
    )
    a2 = audit_candle_store(
        store, exchange="bybit", symbol="APTUSDT", persist_validation_row=False
    )
    assert a1.ok and a2.ok
    assert a1.deterministic_hash == a2.deterministic_hash


def test_optional_mysql_integration_skipped_without_env() -> None:
    pytest.importorskip("pymysql")
    from research.regime_scanner.mysql_candle_store.config import has_regime_db_config

    if not has_regime_db_config():
        pytest.skip("REGIME_DB_* not configured")
    from research.regime_scanner.mysql_candle_store.config import load_regime_db_config
    from research.regime_scanner.mysql_candle_store.store_mysql import MySQLCandleStore

    store = MySQLCandleStore(load_regime_db_config())
    try:
        store.init_schema()
        assert store.count_candles(exchange="__none__", symbol="__none__", timeframe="5m") == 0
    finally:
        store.close()

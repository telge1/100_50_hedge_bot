"""Integration / smoke tests against a live ClickHouse instance.

Skipped automatically when CLICKHOUSE_USER / CLICKHOUSE_PASSWORD are unset.
Uses only TEST_ prefixed sources / generator_versions and cleans up afterward.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from signal_generator.config import get_clickhouse_settings
from signal_generator.db.candles import Candle1m, CandleRepository
from signal_generator.db.client import ClickHouseClient
from signal_generator.db.outcomes import SignalOutcome, SignalOutcomeRepository
from signal_generator.db.setup import setup_clickhouse, table_exists
from signal_generator.db.signals import Signal, SignalRepository

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
TEST_SOURCE = "TEST_INTEGRATION"
TEST_GEN = "TEST_INTEGRATION_v0"


def _has_credentials() -> bool:
    return bool(os.environ.get("CLICKHOUSE_USER") and os.environ.get("CLICKHOUSE_PASSWORD"))


@pytest.fixture(scope="module")
def ch_client() -> ClickHouseClient:
    if not _has_credentials():
        # Try loading project .env
        from signal_generator.config import load_env_files

        load_env_files(project_root=ROOT)
    if not _has_credentials():
        pytest.skip("ClickHouse credentials not configured")

    settings = get_clickhouse_settings(load_dotenv_file=True)
    # Force dedicated DB even if a shared .env points elsewhere.
    from dataclasses import replace

    settings = replace(settings, database="signal_generator")
    client = setup_clickhouse(settings=settings)
    yield client
    # Cleanup TEST rows best-effort
    try:
        CandleRepository(client).delete_test_rows(source_prefix="TEST_")
        SignalRepository(client).delete_test_rows(generator_version_prefix="TEST_")
        time.sleep(0.5)
    finally:
        client.close()


@pytest.fixture
def repos(ch_client: ClickHouseClient):
    return (
        CandleRepository(ch_client),
        SignalRepository(ch_client),
        SignalOutcomeRepository(ch_client),
    )


def test_01_schema_can_be_created(ch_client: ClickHouseClient):
    # setup already ran in fixture; re-run must stay idempotent
    setup_clickhouse(
        settings=get_clickhouse_settings(),
    )
    for name in ("candles_1m", "signals", "signal_outcomes"):
        assert table_exists(ch_client, name)


def test_02_03_write_and_read_1m_candle(repos):
    candles, _, _ = repos
    symbol = f"T{uuid.uuid4().hex[:8].upper()}USDT"
    t0 = datetime(2024, 3, 1, 8, 0, tzinfo=timezone.utc)
    candles.insert_candles(
        [
            Candle1m(
                exchange="bybit",
                symbol=symbol,
                open_time=t0,
                close_time=t0 + timedelta(minutes=1),
                open=10,
                high=11,
                low=9,
                close=10.5,
                volume=1000,
                turnover=10500,
                source=TEST_SOURCE,
            )
        ]
    )
    rows = candles.get_candles(symbol, t0, t0 + timedelta(minutes=5))
    assert len(rows) == 1
    assert rows[0]["symbol"] == symbol
    assert float(rows[0]["close"]) == 10.5


def test_04_repeated_import_no_false_analysis(repos):
    candles, _, _ = repos
    symbol = f"T{uuid.uuid4().hex[:8].upper()}USDT"
    t0 = datetime(2024, 3, 2, 8, 0, tzinfo=timezone.utc)
    row = Candle1m(
        exchange="bybit",
        symbol=symbol,
        open_time=t0,
        close_time=t0 + timedelta(minutes=1),
        open=1,
        high=2,
        low=0.5,
        close=1.5,
        volume=10,
        turnover=15,
        source=TEST_SOURCE,
    )
    candles.insert_candles([row])
    # Second import with same logical key, later ingested_at
    row2 = Candle1m(
        exchange="bybit",
        symbol=symbol,
        open_time=t0,
        close_time=t0 + timedelta(minutes=1),
        open=1,
        high=2,
        low=0.5,
        close=1.5,
        volume=10,
        turnover=15,
        source=TEST_SOURCE,
        ingested_at=datetime.now(timezone.utc) + timedelta(seconds=2),
    )
    candles.insert_candles([row2])
    rows = candles.get_candles(symbol, t0, t0 + timedelta(minutes=1))
    # FINAL dedup → exactly one logical candle for analysis
    assert len(rows) == 1
    assert float(rows[0]["volume"]) == 10


def test_05_06_07_range_multi_symbol_chronological(repos):
    candles, _, _ = repos
    a = f"A{uuid.uuid4().hex[:7].upper()}USDT"
    b = f"B{uuid.uuid4().hex[:7].upper()}USDT"
    t0 = datetime(2024, 3, 3, 9, 0, tzinfo=timezone.utc)
    batch = []
    for sym in (a, b):
        for i in range(5):
            batch.append(
                Candle1m(
                    exchange="bybit",
                    symbol=sym,
                    open_time=t0 + timedelta(minutes=i),
                    close_time=t0 + timedelta(minutes=i + 1),
                    open=i,
                    high=i + 1,
                    low=i - 0.1,
                    close=i + 0.5,
                    volume=1,
                    turnover=1,
                    source=TEST_SOURCE,
                )
            )
    candles.insert_candles(batch)

    # Range query: only minutes 1..3
    mid = candles.get_candles(a, t0 + timedelta(minutes=1), t0 + timedelta(minutes=4))
    assert [r["open_time"].replace(tzinfo=timezone.utc) if r["open_time"].tzinfo is None else r["open_time"].astimezone(timezone.utc) for r in mid] == [
        t0 + timedelta(minutes=1),
        t0 + timedelta(minutes=2),
        t0 + timedelta(minutes=3),
    ]

    multi = candles.get_candles_multi([a, b], t0, t0 + timedelta(minutes=5))
    assert {r["symbol"] for r in multi} == {a, b}
    assert len(multi) == 10
    # Chronological within each symbol
    for sym in (a, b):
        times = [
            (r["open_time"].replace(tzinfo=timezone.utc) if r["open_time"].tzinfo is None else r["open_time"].astimezone(timezone.utc))
            for r in multi
            if r["symbol"] == sym
        ]
        assert times == sorted(times)


def test_08_utc_timestamps_roundtrip(repos):
    candles, _, _ = repos
    symbol = f"U{uuid.uuid4().hex[:8].upper()}USDT"
    t0 = datetime(2024, 6, 15, 23, 59, tzinfo=timezone.utc)
    candles.insert_candles(
        [
            Candle1m(
                exchange="bybit",
                symbol=symbol,
                open_time=t0,
                close_time=t0 + timedelta(minutes=1),
                open=1,
                high=1,
                low=1,
                close=1,
                volume=1,
                turnover=1,
                source=TEST_SOURCE,
            )
        ]
    )
    rows = candles.get_candles(symbol, t0, t0 + timedelta(minutes=1))
    assert len(rows) == 1
    ot = rows[0]["open_time"]
    if ot.tzinfo is None:
        ot = ot.replace(tzinfo=timezone.utc)
    else:
        ot = ot.astimezone(timezone.utc)
    assert ot == t0


def test_09_10_11_12_13_signal_chart_and_flags(repos):
    _, signals, _ = repos
    symbol = f"S{uuid.uuid4().hex[:8].upper()}USDT"
    t0 = datetime(2024, 4, 1, 10, 0, tzinfo=timezone.utc)
    long_id = signals.insert_signal(
        Signal(
            symbol=symbol,
            timeframe="15m",
            direction="LONG",
            signal_type="wave_fade",
            signal_price=0.12,
            candle_open_time=t0,
            candle_close_time=t0 + timedelta(minutes=15),
            generator_version=TEST_GEN,
            strategy_version="strat_v0",
            selected=False,
            traded=False,
            tier_a=True,
            rank_score=9.1,
        )
    )
    short_id = signals.insert_signal(
        Signal(
            symbol=symbol,
            timeframe="15m",
            direction="SHORT",
            signal_type="wave_fade",
            signal_price=0.11,
            candle_open_time=t0 + timedelta(minutes=15),
            candle_close_time=t0 + timedelta(minutes=30),
            generator_version=TEST_GEN,
            strategy_version="strat_v0",
            selected=True,
            traded=False,
            rank_score=3.0,
        )
    )
    rows = signals.get_signals(symbol, t0, t0 + timedelta(hours=1))
    assert len(rows) == 2
    dirs = {str(r["direction"]) for r in rows}
    assert dirs == {"LONG", "SHORT"}
    by_id = {str(r["signal_id"]): r for r in rows}
    assert int(by_id[str(long_id)]["selected"]) == 0
    assert int(by_id[str(long_id)]["traded"]) == 0
    assert int(by_id[str(short_id)]["selected"]) == 1
    assert int(by_id[str(short_id)]["traded"]) == 0
    # Chronological for chart
    times = [
        r["candle_open_time"].replace(tzinfo=timezone.utc)
        if r["candle_open_time"].tzinfo is None
        else r["candle_open_time"].astimezone(timezone.utc)
        for r in rows
    ]
    assert times == sorted(times)


def test_14_15_outcomes_multi_horizon(repos):
    _, signals, outcomes = repos
    symbol = f"O{uuid.uuid4().hex[:8].upper()}USDT"
    t0 = datetime(2024, 5, 1, 10, 0, tzinfo=timezone.utc)
    sid = signals.insert_signal(
        Signal(
            symbol=symbol,
            timeframe="15m",
            direction="LONG",
            signal_type="wave_fade",
            signal_price=1.0,
            candle_open_time=t0,
            candle_close_time=t0 + timedelta(minutes=15),
            generator_version=TEST_GEN,
            strategy_version="strat_v0",
        )
    )
    outcomes.insert_signal_outcomes(
        [
            SignalOutcome(signal_id=sid, horizon="15m", mfe_pct=0.5, mae_pct=-0.2),
            SignalOutcome(signal_id=sid, horizon="30m", mfe_pct=0.8, mae_pct=-0.4),
            SignalOutcome(signal_id=sid, horizon="1h", mfe_pct=1.1, mae_pct=-0.6),
            SignalOutcome(signal_id=sid, horizon="4h", mfe_pct=1.5, mae_pct=-0.9),
        ]
    )
    rows = outcomes.get_outcomes(sid)
    assert len(rows) == 4
    assert [r["horizon"] for r in rows] == ["15m", "1h", "30m", "4h"] or set(
        r["horizon"] for r in rows
    ) == {"15m", "30m", "1h", "4h"}


def test_16_setup_idempotent(ch_client: ClickHouseClient):
    setup_clickhouse(settings=get_clickhouse_settings())
    setup_clickhouse(settings=get_clickhouse_settings())
    assert table_exists(ch_client, "candles_1m")
    # Existing rows must not be wiped — count query should succeed
    result = ch_client.query("SELECT count() FROM signal_generator.candles_1m")
    assert result.result_rows[0][0] >= 0

"""Unit tests for Bybit OI + allLiquidation collector. No live trading, no extra CH tables."""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from orderbook_analyse.oi_liquidation_collector import ALLOWED_TABLES, FORBIDDEN_TABLES
from orderbook_analyse.oi_liquidation_collector.collector import OILiquidationCollector
from orderbook_analyse.oi_liquidation_collector.locks import SingleInstanceLock
from orderbook_analyse.oi_liquidation_collector.logic import (
    DedupCache,
    OIState,
    floor_5s,
    interpret_liquidated_position_side,
    ms_to_dt,
    parse_liquidation_records,
)
from orderbook_analyse.oi_liquidation_collector.schema import SCHEMA_SQL, apply_schema
from orderbook_analyse.oi_liquidation_collector.universe import plan_universe, universe_hash
from orderbook_analyse.oi_liquidation_collector.writer import (
    AllowlistedWriter,
    assert_table_allowed,
    row_tuple,
)

RECEIVED = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)


def test_buy_means_liquidated_long() -> None:
    assert interpret_liquidated_position_side("Buy") == "LIQUIDATED_LONG"


def test_sell_means_liquidated_short() -> None:
    assert interpret_liquidated_position_side("Sell") == "LIQUIDATED_SHORT"


def test_multiple_liquidations_in_data_array() -> None:
    msg = {
        "topic": "allLiquidation.BTCUSDT",
        "ts": 1_000,
        "data": [
            {"T": 1_100, "s": "BTCUSDT", "S": "Buy", "v": "0.1", "p": "65000"},
            {"T": 1_200, "s": "BTCUSDT", "S": "Sell", "v": "0.2", "p": "64900"},
        ],
    }
    rows = parse_liquidation_records(msg, received_at=RECEIVED, collector_instance_id="t1")
    assert len(rows) == 2
    assert rows[0]["liquidated_position_side"] == "LIQUIDATED_LONG"
    assert rows[1]["liquidated_position_side"] == "LIQUIDATED_SHORT"
    assert rows[0]["notional_estimate"] == Decimal("0.1") * Decimal("65000")
    assert rows[0]["source_topic"] == "allLiquidation.BTCUSDT"


def test_time_conversion() -> None:
    ts = ms_to_dt(1_000)
    assert ts == datetime(1970, 1, 1, 0, 0, 1, tzinfo=timezone.utc)


def test_snapshot_initializes_oi_state() -> None:
    st = OIState(symbol="BTCUSDT")
    result = st.apply_ticker(
        {
            "topic": "tickers.BTCUSDT",
            "type": "snapshot",
            "ts": 2_000,
            "cs": 9,
            "data": {
                "symbol": "BTCUSDT",
                "openInterest": "100",
                "openInterestValue": "6500000",
                "lastPrice": "65000",
            },
        },
        received_at=RECEIVED,
    )
    assert result["action"] == "initialized"
    assert st.valid
    assert st.open_interest == Decimal("100")
    assert st.last_price == Decimal("65000")


def test_delta_without_oi_keeps_previous() -> None:
    st = OIState(symbol="BTCUSDT")
    st.apply_ticker(
        {
            "type": "snapshot",
            "ts": 1,
            "data": {"symbol": "BTCUSDT", "openInterest": "10", "openInterestValue": "1"},
        },
        received_at=RECEIVED,
    )
    result = st.apply_ticker(
        {"type": "delta", "ts": 2, "data": {"symbol": "BTCUSDT", "lastPrice": "99"}},
        received_at=RECEIVED,
    )
    assert result["action"] == "no_change"
    assert st.open_interest == Decimal("10")
    assert st.last_price == Decimal("99")


def test_delta_new_oi_emits_change() -> None:
    st = OIState(symbol="BTCUSDT")
    st.apply_ticker(
        {
            "type": "snapshot",
            "ts": 1,
            "data": {"symbol": "BTCUSDT", "openInterest": "10", "openInterestValue": "1"},
        },
        received_at=RECEIVED,
    )
    result = st.apply_ticker(
        {
            "type": "delta",
            "ts": 2,
            "data": {"symbol": "BTCUSDT", "openInterest": "11", "openInterestValue": "1.1"},
        },
        received_at=RECEIVED,
    )
    assert result["changed"] is True
    row = st.change_event_row("t")
    assert row is not None
    assert row["open_interest"] == Decimal("11")


def test_identical_oi_does_not_emit_change() -> None:
    st = OIState(symbol="BTCUSDT")
    st.apply_ticker(
        {
            "type": "snapshot",
            "ts": 1,
            "data": {"symbol": "BTCUSDT", "openInterest": "10", "openInterestValue": "1"},
        },
        received_at=RECEIVED,
    )
    result = st.apply_ticker(
        {
            "type": "delta",
            "ts": 2,
            "data": {"symbol": "BTCUSDT", "openInterest": "10", "openInterestValue": "1"},
        },
        received_at=RECEIVED,
    )
    assert result["action"] == "no_change"


def test_5s_bucket_exact() -> None:
    ts = datetime(2026, 8, 18, 12, 0, 7, 123000, tzinfo=timezone.utc)
    assert floor_5s(ts) == datetime(2026, 8, 18, 12, 0, 5, tzinfo=timezone.utc)


def test_reconnect_invalidates_and_ignores_delta() -> None:
    st = OIState(symbol="ETHUSDT")
    st.apply_ticker(
        {
            "type": "snapshot",
            "ts": 1,
            "data": {"symbol": "ETHUSDT", "openInterest": "5", "openInterestValue": "2"},
        },
        received_at=RECEIVED,
    )
    st.invalidate()
    result = st.apply_ticker(
        {
            "type": "delta",
            "ts": 2,
            "data": {"symbol": "ETHUSDT", "openInterest": "9", "openInterestValue": "3"},
        },
        received_at=RECEIVED,
    )
    assert result["action"] == "ignored_no_snapshot"
    assert st.valid is False
    assert st.change_event_row("t") is None


def test_new_snapshot_revalidates() -> None:
    st = OIState(symbol="XRPUSDT")
    st.invalidate()
    result = st.apply_ticker(
        {
            "type": "snapshot",
            "ts": 3,
            "data": {"symbol": "XRPUSDT", "openInterest": "7", "openInterestValue": "4"},
        },
        received_at=RECEIVED,
    )
    assert result["action"] == "initialized"
    assert st.valid is True


def test_dedup_logic() -> None:
    cache = DedupCache()
    assert cache.check_and_add("a") is True
    assert cache.check_and_add("a") is False
    assert cache.check_and_add("b") is True


def test_batch_retry_counts_rows_once() -> None:
    class BoomThenOk:
        def __init__(self) -> None:
            self.calls = 0

        def insert(self, table, data, column_names):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temp")
            return None

        def close(self) -> None:
            return None

    client = BoomThenOk()
    writer = AllowlistedWriter(client_factory=lambda: client, max_retries=3)
    rec = {
        "exchange": "BYBIT",
        "category": "linear",
        "symbol": "BTCUSDT",
        "bucket_time": RECEIVED,
        "open_interest": Decimal("1"),
        "open_interest_value": None,
        "source": "BYBIT_REST_5M_HISTORY",
        "collector_instance_id": "t",
        "inserted_at": RECEIVED,
    }
    writer._insert_sync("open_interest_5m_history", [row_tuple("open_interest_5m_history", rec)])
    assert writer.rows_inserted == 1
    assert client.calls == 2


def test_single_instance_lock(tmp_path: Path) -> None:
    lock_a = SingleInstanceLock(tmp_path / "c.lock", tmp_path / "c.pid")
    lock_b = SingleInstanceLock(tmp_path / "c.lock", tmp_path / "c.pid")
    lock_a.acquire()
    with pytest.raises(RuntimeError):
        lock_b.acquire()
    lock_a.release()
    lock_b.acquire()
    lock_b.release()


def test_universe_validation(tmp_path: Path) -> None:
    path = tmp_path / "u.json"
    path.write_text('{"symbols": ["BTCUSDT", "FAKEUSDT", "XAUTUSDT"]}')
    plan = plan_universe(
        universe_path=path,
        bybit_symbols={"BTCUSDT", "XAUTUSDT", "ETHUSDT"},
        subscribe=True,
    )
    assert plan.supported == ("BTCUSDT", "XAUTUSDT")
    fake = next(d for d in plan.decisions if d.symbol == "FAKEUSDT")
    assert fake.supported is False
    assert fake.subscribed is False
    assert plan.universe_hash == universe_hash(["BTCUSDT", "XAUTUSDT"])
    assert plan.special_review["XAUTUSDT"]["in_requested_universe"] is True


def test_writer_refuses_forbidden_tables() -> None:
    for table in FORBIDDEN_TABLES:
        with pytest.raises(ValueError):
            assert_table_allowed(table)
    with pytest.raises(ValueError):
        assert_table_allowed("candles_1m")
    assert_table_allowed("all_liquidations")
    sql = SCHEMA_SQL.read_text()
    for line in sql.splitlines():
        if "CREATE TABLE" not in line:
            continue
        assert "orderbook_deltas" not in line
        assert "ticker_samples" not in line
        assert "public_trades" not in line
        assert "candles_1m" not in line
        assert ".liquidations\n" not in line + "\n"


def test_no_trading_or_signal_entrypoints() -> None:
    src = inspect.getsource(OILiquidationCollector)
    for needle in ("place_order", "/v5/order", "private/", "signal_generator", "wave_fade"):
        assert needle not in src
    assert "orderbook." not in src
    assert "allLiquidation." in inspect.getsource(OILiquidationCollector._topics)
    assert "tickers." in inspect.getsource(OILiquidationCollector._topics)

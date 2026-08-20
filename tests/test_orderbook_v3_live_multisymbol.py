"""Multi-symbol hardening tests for the Orderbook V3 live collector."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from orderbook_analyse.orderbook_v2 import PARSER_VERSION
from orderbook_analyse.orderbook_v2_live.clock import LiveSecondClock, SequenceBreak
from orderbook_analyse.orderbook_v2_live.collector import OrderbookV3LiveCollector
from orderbook_analyse.orderbook_v2_live.settings import LiveCollectorConfigError, load_live_settings
from orderbook_analyse.orderbook_v2_live.skip_before import load_skip_map, skip_before_from_last_db
from orderbook_analyse.orderbook_v2_live.subscribe import chunk_topics
from orderbook_analyse.orderbook_v2_live.universe import (
    FORBIDDEN_SYMBOLS,
    SHADOW3_SYMBOLS,
    SYMBOLS_51,
    symbols_for_mode,
    validate_universe,
)
from orderbook_analyse.orderbook_v2_live.writer import FeatureWriter, QueueFullError

T0 = 1_700_000_000_000


def _book(u: int) -> dict:
    return {"b": [["1.0", "10"]], "a": [["1.1", "10"]], "u": u, "seq": u}


def _collector(mode: str = "shadow3") -> OrderbookV3LiveCollector:
    settings = load_live_settings(mode=mode, confirm_universe_51=(mode == "universe51"))
    coll = OrderbookV3LiveCollector(settings, client_factory=lambda: None, duration_sec=0)
    coll._reset_runtimes({})
    for rt in coll.runtimes.values():
        rt.active_generation = rt.clock.generation
        rt.dropping_until_subscribe_ack = False
        rt.subscription_confirmed = True
        rt.subscribed = True
    return coll


def test_universe_51_exact_no_xau_has_xaut():
    validate_universe(SYMBOLS_51)
    assert len(SYMBOLS_51) == 51
    assert len(set(SYMBOLS_51)) == 51
    assert "XAUUSDT" not in SYMBOLS_51
    assert "XAUUSDT" in FORBIDDEN_SYMBOLS
    assert "XAUTUSDT" in SYMBOLS_51
    assert SYMBOLS_51[:3] == SHADOW3_SYMBOLS
    assert symbols_for_mode("ada") == ("ADAUSDT",)
    assert symbols_for_mode("shadow3") == SHADOW3_SYMBOLS


def test_universe51_requires_confirm():
    with pytest.raises(LiveCollectorConfigError, match="confirm-universe-51"):
        load_live_settings(mode="universe51", confirm_universe_51=False)
    settings = load_live_settings(mode="universe51", confirm_universe_51=True)
    assert settings.symbols == SYMBOLS_51
    assert PARSER_VERSION == "ob200_v3"


def test_chunk_topics_3_10_11_51():
    def topics(n):
        return [f"orderbook.200.S{i}" for i in range(n)]

    assert chunk_topics(topics(3), 10) == [topics(3)]
    c10 = chunk_topics(topics(10), 10)
    assert len(c10) == 1 and len(c10[0]) == 10
    c11 = chunk_topics(topics(11), 10)
    assert [len(x) for x in c11] == [10, 1]
    c51 = chunk_topics(topics(51), 10)
    assert [len(x) for x in c51] == [10, 10, 10, 10, 10, 1]
    assert chunk_topics(["a", "a", "b"], 10) == [["a", "b"]]


def test_chunk_ack_error_raises_dead_connection():
    from orderbook_analyse.orderbook_v2_live.collector import DeadConnection

    coll = _collector("ada")

    class WS:
        def __init__(self):
            self.sent = []

        async def send(self, raw):
            self.sent.append(raw)

        async def recv(self):
            return b'{"op":"subscribe","success":false,"ret_msg":"too many topics"}'

    async def run():
        with pytest.raises(DeadConnection, match="subscribe_rejected"):
            await coll._send_chunk(WS(), "subscribe", ["orderbook.200.ADAUSDT"])

    asyncio.run(run())


def test_separate_books_and_sequences():
    coll = _collector()
    now = datetime.now(timezone.utc)
    for i, sym in enumerate(SHADOW3_SYMBOLS):
        payload = {
            "topic": f"orderbook.200.{sym}",
            "type": "snapshot",
            "ts": T0,
            "data": {**_book(10 + i), "s": sym},
        }
        coll.handle_orderbook_message(payload, now)
    assert coll.runtimes["ADAUSDT"].clock.book.last_u == 10
    assert coll.runtimes["BTCUSDT"].clock.book.last_u == 11
    assert coll.runtimes["ETHUSDT"].clock.book.last_u == 12
    coll.handle_orderbook_message(
        {"topic": "orderbook.200.ADAUSDT", "type": "delta", "ts": T0 + 10,
         "data": {"s": "ADAUSDT", "b": [["1.0", "11"]], "a": [], "u": 11, "seq": 11}},
        now,
    )
    assert coll.runtimes["ADAUSDT"].clock.book.bids[__import__("decimal").Decimal("1.0")] == __import__("decimal").Decimal("11")
    assert coll.runtimes["BTCUSDT"].clock.book.last_u == 11


def test_sequence_break_isolates_only_a():
    coll = _collector()
    now = datetime.now(timezone.utc)
    for sym in SHADOW3_SYMBOLS:
        coll.handle_orderbook_message(
            {"topic": f"orderbook.200.{sym}", "type": "snapshot", "ts": T0,
             "data": {**_book(1), "s": sym}},
            now,
        )
    coll.handle_orderbook_message(
        {"topic": "orderbook.200.ADAUSDT", "type": "delta", "ts": T0 + 10,
         "data": {"s": "ADAUSDT", "b": [], "a": [], "u": 99, "seq": 99}},
        now,
    )
    assert coll.runtimes["ADAUSDT"].clock.waiting_for_snapshot
    assert coll.runtimes["ADAUSDT"].symbol_resyncs == 1
    assert coll.runtimes["ADAUSDT"].dropping_until_subscribe_ack
    assert not coll.runtimes["BTCUSDT"].clock.waiting_for_snapshot
    assert not coll.runtimes["ETHUSDT"].clock.waiting_for_snapshot
    btc_u = coll.runtimes["BTCUSDT"].clock.book.last_u
    coll.handle_orderbook_message(
        {"topic": "orderbook.200.BTCUSDT", "type": "delta", "ts": T0 + 20,
         "data": {"s": "BTCUSDT", "b": [["1.0", "12"]], "a": [], "u": 2, "seq": 2}},
        now,
    )
    assert coll.runtimes["BTCUSDT"].clock.book.last_u == 2
    assert btc_u == 1
    # ADA deltas dropped until resubscribe ack
    before = len(coll.runtimes["ADAUSDT"].pending_raw)
    coll.handle_orderbook_message(
        {"topic": "orderbook.200.ADAUSDT", "type": "delta", "ts": T0 + 30,
         "data": {"s": "ADAUSDT", "b": [["1.0", "99"]], "a": [], "u": 100, "seq": 100}},
        now,
    )
    assert len(coll.runtimes["ADAUSDT"].pending_raw) > before


def test_old_generation_ignored_then_new_snapshot_required():
    clock = LiveSecondClock("ADAUSDT")
    clock.ingest("snapshot", T0, _book(1), generation=0)
    gen = clock.begin_resync()
    assert gen == 1
    assert clock.waiting_for_snapshot
    rows = clock.ingest("snapshot", T0 + 1000, _book(5), generation=0)
    assert rows == []
    assert clock.stale_generation_dropped >= 1
    assert clock.waiting_for_snapshot
    clock.ingest("snapshot", T0 + 2000, _book(50), generation=1)
    assert not clock.waiting_for_snapshot
    assert clock.book.last_u == 50


def test_skip_before_independent_per_symbol():
    now = datetime(2026, 8, 19, 18, 0, tzinfo=timezone.utc)
    ada_last = datetime(2026, 8, 19, 17, 30, tzinfo=timezone.utc)
    class FakeCH:
        def query(self, sql, parameters=None):
            sym = parameters["sym"]
            val = {"ADAUSDT": ada_last, "BTCUSDT": None, "ETHUSDT": None}[sym]
            return SimpleNamespace(result_rows=[[val]])

    mapping = load_skip_map(FakeCH(), SHADOW3_SYMBOLS, now=now)
    assert mapping["ADAUSDT"]["skip_before_ms"] == int(ada_last.timestamp() * 1000) + 1000
    assert mapping["BTCUSDT"]["skip_before_ms"] is None
    assert mapping["ETHUSDT"]["skip_before_ms"] is None
    assert mapping["ADAUSDT"]["catchup_required"] is True


def test_skip_before_future_and_db_error_do_not_spread():
    now = datetime(2026, 8, 19, 18, 0, tzinfo=timezone.utc)
    future = now + timedelta(hours=2)
    row = skip_before_from_last_db(future, now=now)
    assert row["skip_before_ms"] is None
    assert row["error"] == "future_last_db_bucket"

    class Boom:
        def query(self, *a, **k):
            if k["parameters"]["sym"] == "BTCUSDT":
                raise RuntimeError("ch down")
            return SimpleNamespace(result_rows=[[None]])

    mapping = load_skip_map(Boom(), SHADOW3_SYMBOLS, now=now)
    assert mapping["ADAUSDT"]["error"] is None
    assert mapping["BTCUSDT"]["error"].startswith("db_read_failed")
    assert mapping["ETHUSDT"]["error"] is None
    assert mapping["ADAUSDT"]["skip_before_ms"] is None


def test_health_aggregates_all_symbols_not_index_zero():
    coll = _collector()
    now = datetime.now(timezone.utc)
    coll.handle_orderbook_message(
        {"topic": "orderbook.200.BTCUSDT", "type": "snapshot", "ts": T0,
         "data": {**_book(1), "s": "BTCUSDT"}},
        now,
    )
    h = coll.health_payload()
    assert h["configured_symbols"] == list(SHADOW3_SYMBOLS)
    by_sym = {row["symbol"]: row for row in h["per_symbol"]}
    assert by_sym["ADAUSDT"]["snapshot_received"] is False
    assert by_sym["BTCUSDT"]["snapshot_received"] is True
    assert by_sym["BTCUSDT"]["book_valid"] is True
    assert h["valid_books"] == 1
    assert h["waiting_for_snapshot"] == 2
    assert h["queue_capacity"] == 2048


def test_one_logical_key_per_symbol_second():
    coll = _collector("ada")
    now = datetime.now(timezone.utc)
    rt = coll.runtimes["ADAUSDT"]
    coll.handle_orderbook_message(
        {"topic": "orderbook.200.ADAUSDT", "type": "snapshot", "ts": T0,
         "data": {**_book(1), "s": "ADAUSDT"}},
        now,
    )
    rows = rt.clock.close_through(T0 + 2000)
    coll._enqueue_rows("ADAUSDT", rows)
    again = rt.clock.close_through(T0 + 2000)
    assert again == []


def test_writer_batch_size_and_time_flush_and_retry():
    written = []
    attempts = {"n": 0}

    class FakeCH:
        def insert(self, table, data, column_names=None):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("temp")
            written.extend(data)

    async def run():
        w = FeatureWriter(
            lambda: FakeCH(),
            queue_capacity=16,
            insert_batch_size=3,
            flush_interval_sec=0.05,
            insert_retry_count=3,
            insert_fn=lambda client, batch: client.insert("t", batch),
        )
        task = asyncio.create_task(w.run())
        w.enqueue([{"exchange": "bybit"}] * 3)
        await asyncio.sleep(1.0)
        assert len(written) == 3
        assert w.batches_flushed == 1
        w.enqueue([{"exchange": "bybit"}])
        await asyncio.sleep(0.3)
        assert len(written) == 4
        ok = await w.join(task, timeout=2)
        assert ok
        assert attempts["n"] >= 2

    asyncio.run(run())


def test_queue_full_fail_closed_no_silent_drop():
    w = FeatureWriter(lambda: None, queue_capacity=1, insert_batch_size=100, flush_interval_sec=30)
    w.enqueue([{"a": 1}])
    with pytest.raises(QueueFullError):
        w.enqueue([{"a": 2}])
    assert w.state == "FAIL_CLOSED"
    assert w.queue.qsize() == 1


def test_shutdown_flush_and_timeout():
    class SlowCH:
        def insert(self, *a, **k):
            import time
            time.sleep(0.4)

    async def run_ok():
        w = FeatureWriter(
            lambda: SlowCH(),
            queue_capacity=8,
            insert_batch_size=10,
            flush_interval_sec=1,
            shutdown_flush_timeout_sec=2,
            insert_fn=lambda client, batch: client.insert("t", batch),
        )
        task = asyncio.create_task(w.run())
        w.enqueue([{"x": 1}])
        ok = await w.join(task, timeout=2)
        assert ok

    async def run_timeout():
        w = FeatureWriter(
            lambda: SlowCH(),
            queue_capacity=8,
            insert_batch_size=10,
            flush_interval_sec=1,
            insert_fn=lambda client, batch: client.insert("t", batch),
        )
        task = asyncio.create_task(w.run())
        w.enqueue([{"x": 1}])
        ok = await w.join(task, timeout=0.05)
        assert ok is False
        assert w.last_error == "shutdown_flush_timeout"

    asyncio.run(run_ok())
    asyncio.run(run_timeout())


def test_reconnect_resubscribe_chunk_plan():
    settings = load_live_settings(mode="universe51", confirm_universe_51=True)
    chunks = chunk_topics(settings.orderbook_topics(), 10)
    assert sum(len(c) for c in chunks) == 51
    assert max(len(c) for c in chunks) == 10

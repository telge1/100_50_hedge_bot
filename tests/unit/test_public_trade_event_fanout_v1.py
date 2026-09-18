"""Unit tests for PublicTradeEventFanout (bounded, fail-closed, no second WS)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal

from signal_generator.bybit.live.public_trade_event_fanout import PublicTradeEventFanout
from signal_generator.bybit.live.ws_public_trade import WsPublicTrade


def _trade(tid: str, *, symbol: str = "BTCUSDT", ts_ms: int = 1_700_000_000_000) -> WsPublicTrade:
    return WsPublicTrade(
        symbol=symbol,
        trade_id=tid,
        trade_ts=datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc),
        side="Buy",
        price=Decimal("100"),
        size=Decimal("0.1"),
        notional=Decimal("10"),
        tick_direction="PlusTick",
        is_rpi_trade=0,
    )


def test_one_subscriber_order_and_receive_time():
    hub = PublicTradeEventFanout(default_queue_size=64)
    assert hub.second_bybit_trade_ws is False
    sid = hub.create_subscriber(symbol="BTCUSDT")["subscriber_id"]
    for i in range(1, 4):
        hub.on_trade(_trade(str(i), ts_ms=1_700_000_000_000 + i), receive_time_ns=1000 + i)
    polled = hub.poll_events(subscriber_id=sid, limit=100)
    assert polled["ok"]
    assert polled["coverage_valid"] is True
    ev = polled["events"]
    assert len(ev) == 3
    assert [e["trade_id"] for e in ev] == ["1", "2", "3"]
    assert all(e.get("receive_time_ns") for e in ev)
    assert all(e.get("exchange_event_time") for e in ev)
    hub.remove_subscriber(sid)


def test_multi_subscriber_isolated():
    hub = PublicTradeEventFanout()
    a = hub.create_subscriber(symbol="BTCUSDT")["subscriber_id"]
    b = hub.create_subscriber(symbol="BTCUSDT")["subscriber_id"]
    hub.on_trade(_trade("x"), receive_time_ns=1)
    assert len(hub.poll_events(subscriber_id=a)["events"]) == 1
    assert len(hub.poll_events(subscriber_id=b)["events"]) == 1
    hub.remove_subscriber(a)
    hub.remove_subscriber(b)


def test_symbol_filter():
    hub = PublicTradeEventFanout()
    sid = hub.create_subscriber(symbols=["ETHUSDT"])["subscriber_id"]
    hub.on_trade(_trade("1", symbol="BTCUSDT"), receive_time_ns=1)
    hub.on_trade(_trade("2", symbol="ETHUSDT"), receive_time_ns=2)
    ev = hub.poll_events(subscriber_id=sid, limit=10)["events"]
    assert len(ev) == 1
    assert ev[0]["symbol"] == "ETHUSDT"
    hub.remove_subscriber(sid)


def test_slow_subscriber_overflow_fail_closed_nonblocking():
    hub = PublicTradeEventFanout(default_queue_size=4)
    sid = hub.create_subscriber(symbol="BTCUSDT", max_queue=4)["subscriber_id"]
    t0 = time.perf_counter()
    for i in range(50):
        hub.on_trade(_trade(str(i)), receive_time_ns=i)
    assert time.perf_counter() - t0 < 1.0
    cov = hub.heartbeat(sid)["coverage"]
    assert cov["overflow"] is True
    assert cov["coverage_valid"] is False
    assert cov["dropped_count"] > 0
    polled = hub.poll_events(subscriber_id=sid, limit=100)
    assert polled["coverage_valid"] is False
    hub.remove_subscriber(sid)


def test_cursor_fail_closed_and_has_more():
    hub = PublicTradeEventFanout()
    sid = hub.create_subscriber(symbol="BTCUSDT", max_queue=100)["subscriber_id"]
    for i in range(10):
        hub.on_trade(_trade(str(i)), receive_time_ns=i)
    bad = hub.poll_events(subscriber_id=sid, cursor=99)
    assert bad["ok"] is False
    assert bad["coverage_valid"] is False
    p1 = hub.poll_events(subscriber_id=sid, limit=3)
    assert p1["ok"] and p1["has_more"] is True
    hub.remove_subscriber(sid)


def test_same_ts_different_trade_ids_and_dedupe():
    hub = PublicTradeEventFanout()
    sid = hub.create_subscriber(symbol="BTCUSDT")["subscriber_id"]
    ts = 1_700_000_000_500
    hub.on_trade(_trade("a", ts_ms=ts), receive_time_ns=1)
    hub.on_trade(_trade("b", ts_ms=ts), receive_time_ns=2)
    hub.on_trade(_trade("a", ts_ms=ts), receive_time_ns=3)  # dup
    ev = hub.poll_events(subscriber_id=sid, limit=10)["events"]
    assert [e["trade_id"] for e in ev] == ["a", "b"]
    hub.remove_subscriber(sid)


def test_heartbeat_timeout_cleanup_idempotent_remove_shutdown():
    hub = PublicTradeEventFanout(subscriber_ttl_sec=0.05)
    sid = hub.create_subscriber(symbol="BTCUSDT")["subscriber_id"]
    assert hub.remove_subscriber(sid)["removed"] is True
    assert hub.remove_subscriber(sid)["removed"] is False
    sid2 = hub.create_subscriber(symbol="BTCUSDT")["subscriber_id"]
    time.sleep(0.08)
    cleaned = hub.timeout_cleanup()
    assert sid2 in cleaned["removed"]
    s3 = hub.create_subscriber(symbol="ETHUSDT")["subscriber_id"]
    hub.shutdown()
    assert hub.poll_events(subscriber_id=s3)["ok"] is False


def test_reconnect_generation():
    hub = PublicTradeEventFanout()
    sid = hub.create_subscriber(symbol="BTCUSDT")["subscriber_id"]
    g0 = hub.status()["connection_generation"]
    g1 = hub.note_reconnect()
    assert g1 > g0
    hub.on_trade(_trade("1"), receive_time_ns=1)
    ev = hub.poll_events(subscriber_id=sid)["events"]
    assert ev[0]["connection_generation"] == g1
    hub.remove_subscriber(sid)


def test_subscriber_exception_isolated_via_on_trade():
    hub = PublicTradeEventFanout()
    sid = hub.create_subscriber(symbol="BTCUSDT")["subscriber_id"]

    class Boom:
        symbol = "BTCUSDT"
        trade_id = "z"

        def __getattribute__(self, name):
            if name in {"price", "size", "notional", "side", "trade_ts", "tick_direction", "is_rpi_trade"}:
                raise RuntimeError("boom")
            return object.__getattribute__(self, name)

    # should not raise
    hub.on_trade(Boom(), receive_time_ns=1)  # type: ignore[arg-type]
    hub.on_trade(_trade("ok"), receive_time_ns=2)
    ev = hub.poll_events(subscriber_id=sid)["events"]
    assert any(e["trade_id"] == "ok" for e in ev)
    hub.remove_subscriber(sid)

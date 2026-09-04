"""Tests for on-demand OB1000 Unix socket, lease, and snapshot modules."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orderbook_analyse.orderbook_v2.book import apply_snapshot
from orderbook_analyse.orderbook_v2_live.depth import (
    DEFAULT_DEPTH,
    orderbook_topic,
    parse_orderbook_topic,
    validate_depth,
)
from orderbook_analyse.orderbook_v2_live.on_demand_lease import (
    ON_DEMAND_DEPTH,
    LeaseKey,
    LeaseManager,
)
from orderbook_analyse.orderbook_v2_live.on_demand_manager import OnDemandDepthManager, load_on_demand_settings
from orderbook_analyse.orderbook_v2_live.on_demand_snapshot import SOURCE_NAME, build_snapshot_payload
from orderbook_analyse.orderbook_v2_live.on_demand_socket import OnDemandSocketServer, prepare_socket_path


def _manager_settings(tmp_path: Path, **overrides) -> dict:
    base = {
        "enabled": True,
        "max_active_topics": 4,
        "heartbeat_sec": 15,
        "lease_ttl_sec": 45,
        "socket_path": tmp_path / "ob1000.sock",
        "pilot_symbols": {"BTCUSDT", "DOGEUSDT"},
    }
    base.update(overrides)
    return base


async def _socket_request(path: Path, req: dict) -> dict:
    data = (json.dumps(req, separators=(",", ":")) + "\n").encode("utf-8")
    reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(path)), timeout=2.0)
    writer.write(data)
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=2.0)
    writer.close()
    await writer.wait_closed()
    return json.loads(line.decode("utf-8"))


async def _start_socket(mgr: OnDemandDepthManager) -> OnDemandSocketServer:
    server = OnDemandSocketServer(mgr.socket_path, mgr.handle_request)
    await server.start()
    return server


def test_default_depth_unchanged():
    assert DEFAULT_DEPTH == 200
    assert validate_depth(200) == 200
    assert orderbook_topic("BTCUSDT", 200) == "orderbook.200.BTCUSDT"
    assert orderbook_topic("BTCUSDT", 1000) == "orderbook.1000.BTCUSDT"
    assert parse_orderbook_topic("orderbook.1000.DOGEUSDT") == ("DOGEUSDT", 1000)


def test_load_on_demand_disabled_by_default(monkeypatch):
    monkeypatch.delenv("OB_V3_ON_DEMAND_ENABLE", raising=False)
    cfg = load_on_demand_settings()
    assert cfg["enabled"] is False


def test_first_lease_subscribes_once():
    mgr = LeaseManager(max_active_topics=2)
    _, sub1 = mgr.acquire(symbol="BTCUSDT", session_id="s1", lease_id="l1")
    _, sub2 = mgr.acquire(symbol="BTCUSDT", session_id="s2", lease_id="l2")
    assert sub1 is True
    assert sub2 is False


def test_acquire_idempotent_same_lease_id():
    mgr = LeaseManager(max_active_topics=2)
    lease1, sub1 = mgr.acquire(symbol="BTCUSDT", session_id="s1", lease_id="tab-1")
    lease2, sub2 = mgr.acquire(symbol="BTCUSDT", session_id="s1", lease_id="tab-1")
    assert sub1 is True
    assert sub2 is False
    assert lease1.lease_id == lease2.lease_id
    assert mgr.active_count(LeaseKey("BTCUSDT", ON_DEMAND_DEPTH)) == 1


def test_heartbeat_rejects_foreign_symbol():
    mgr = LeaseManager()
    mgr.acquire(symbol="BTCUSDT", session_id="s1", lease_id="l1")
    with pytest.raises(ValueError, match="lease_symbol_mismatch"):
        mgr.heartbeat("l1", symbol="DOGEUSDT")


def test_heartbeat_unknown_lease_not_created():
    mgr = LeaseManager()
    with pytest.raises(KeyError):
        mgr.heartbeat("missing")


def test_release_one_of_two_keeps_topic():
    mgr = LeaseManager()
    l1, _ = mgr.acquire(symbol="BTCUSDT", session_id="a", lease_id="a")
    l2, _ = mgr.acquire(symbol="BTCUSDT", session_id="b", lease_id="b")
    key, unsub = mgr.release(l1.lease_id)
    assert key == LeaseKey("BTCUSDT", ON_DEMAND_DEPTH)
    assert unsub is False
    _, unsub2 = mgr.release(l2.lease_id)
    assert unsub2 is True


def test_capacity_reached():
    mgr = LeaseManager(max_active_topics=1)
    mgr.acquire(symbol="BTCUSDT", session_id="a", lease_id="a")
    with pytest.raises(RuntimeError, match="capacity_reached"):
        mgr.acquire(symbol="DOGEUSDT", session_id="b", lease_id="b")


def test_snapshot_more_than_200_levels():
    book = apply_snapshot(
        {
            "b": [[str(1000 - i * 0.1), str(i + 1)] for i in range(250)],
            "a": [[str(1000 + i * 0.1 + 1), str(i + 1)] for i in range(250)],
            "u": 1,
            "seq": 1,
        }
    )
    payload = build_snapshot_payload(
        symbol="BTCUSDT",
        depth=1000,
        book=book,
        timestamp_utc=datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc),
        subscription_state="live",
        freshness_state="fresh",
        freshness_ms=100,
    )
    assert payload["depth"] == 1000
    assert payload["bid_levels"] > 200
    assert payload["source"] == SOURCE_NAME
    assert payload["bids"][0]["price"] > payload["bids"][1]["price"]
    assert payload["asks"][0]["price"] < payload["asks"][1]["price"]
    assert payload["best_bid"] < payload["best_ask"]


def test_snapshot_requires_source_timestamp():
    book = apply_snapshot({"b": [["1", "1"]], "a": [["2", "1"]], "u": 1, "seq": 1})
    with pytest.raises(ValueError, match="missing_source_timestamp"):
        build_snapshot_payload(
            symbol="BTCUSDT",
            depth=1000,
            book=book,
            timestamp_utc=None,
            subscription_state="live",
        )


def test_socket_start_shutdown_and_permissions(tmp_path: Path):
    async def _run() -> None:
        sock_path = tmp_path / "ob1000.sock"
        mgr = OnDemandDepthManager(
            exchange="bybit",
            market="linear",
            send_chunk=lambda *a, **k: asyncio.sleep(0),
            confirmed_topics=[],
            settings=_manager_settings(tmp_path, socket_path=sock_path),
        )
        server = await _start_socket(mgr)
        assert sock_path.is_socket()
        assert stat.S_IMODE(sock_path.stat().st_mode) == 0o600
        resp = await _socket_request(sock_path, {"request_id": "r1", "operation": "status", "depth": 1000})
        assert resp["request_id"] == "r1"
        assert resp["ok"] is True
        await server.stop()
        assert not sock_path.exists()

    asyncio.run(_run())


def test_socket_invalid_json_and_unknown_operation(tmp_path: Path):
    async def _run() -> None:
        mgr = OnDemandDepthManager(
            exchange="bybit",
            market="linear",
            send_chunk=lambda *a, **k: asyncio.sleep(0),
            confirmed_topics=[],
            settings=_manager_settings(tmp_path),
        )
        server = await _start_socket(mgr)
        try:
            resp = await _socket_request(
                mgr.socket_path,
                {"request_id": "bad", "operation": "nope", "depth": 1000},
            )
            assert resp["ok"] is False
            assert resp["error"] == "unknown_operation"
            resp2 = await _socket_request(
                mgr.socket_path,
                {"request_id": "depth", "operation": "status", "depth": 200},
            )
            assert resp2["ok"] is False
            assert resp2["error"] == "only_depth_1000_supported"
        finally:
            await server.stop()

    asyncio.run(_run())


def test_socket_acquire_heartbeat_release(tmp_path: Path):
    async def _run() -> None:
        confirmed: list[str] = []
        calls: list[tuple[str, list[str]]] = []

        async def send_chunk(ws, op, args):
            calls.append((op, list(args)))
            confirmed.extend(args)

        mgr = OnDemandDepthManager(
            exchange="bybit",
            market="linear",
            send_chunk=send_chunk,
            confirmed_topics=confirmed,
            settings=_manager_settings(tmp_path),
        )
        server = await _start_socket(mgr)
        try:
            acquire = await _socket_request(
                mgr.socket_path,
                {
                    "request_id": "a1",
                    "operation": "acquire",
                    "lease_id": "tab-1",
                    "symbol": "BTCUSDT",
                    "depth": 1000,
                },
            )
            assert acquire["ok"] is True
            assert acquire["subscription_state"] == "starting"
            await mgr.tick(object())
            assert ("subscribe", ["orderbook.1000.BTCUSDT"]) in calls
            hb = await _socket_request(
                mgr.socket_path,
                {
                    "request_id": "h1",
                    "operation": "heartbeat",
                    "lease_id": "tab-1",
                    "symbol": "BTCUSDT",
                    "depth": 1000,
                },
            )
            assert hb["ok"] is True
            rel = await _socket_request(
                mgr.socket_path,
                {
                    "request_id": "r1",
                    "operation": "release",
                    "lease_id": "tab-1",
                    "depth": 1000,
                },
            )
            assert rel["ok"] is True
            assert rel["subscription_state"] == "grace"
        finally:
            await server.stop()

    asyncio.run(_run())


def test_socket_snapshot_in_memory(tmp_path: Path):
    async def _run() -> None:
        mgr = OnDemandDepthManager(
            exchange="bybit",
            market="linear",
            send_chunk=lambda *a, **k: asyncio.sleep(0),
            confirmed_topics=[],
            settings=_manager_settings(tmp_path),
        )
        mgr.leases.acquire(symbol="BTCUSDT", session_id="tab", lease_id="tab")
        rt = mgr._get_runtime("BTCUSDT", 1000)
        rt.subscription_state = "live"
        rt.last_event_timestamp = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)
        rt.clock.ingest(
            "snapshot",
            int(rt.last_event_timestamp.timestamp() * 1000),
            {
                "s": "BTCUSDT",
                "b": [[str(1000 - i * 0.1), str(i + 1)] for i in range(220)],
                "a": [[str(1000 + i * 0.1 + 1), str(i + 1)] for i in range(220)],
                "u": 1,
                "seq": 1,
            },
            generation=rt.clock.generation,
        )
        server = await _start_socket(mgr)
        try:
            snap = await _socket_request(
                mgr.socket_path,
                {
                    "request_id": "s1",
                    "operation": "snapshot",
                    "symbol": "BTCUSDT",
                    "depth": 1000,
                    "lease_id": "tab",
                },
            )
            assert snap["ok"] is True
            assert snap["source"] == SOURCE_NAME
            assert len(snap["bids"]) > 200
            assert snap["timestamp_utc"] is not None
        finally:
            await server.stop()

    asyncio.run(_run())


def test_manager_subscribe_once_for_two_leases(tmp_path: Path):
    async def _run() -> None:
        confirmed: list[str] = []
        calls: list[tuple[str, list[str]]] = []

        async def send_chunk(ws, op, args):
            calls.append((op, list(args)))
            confirmed.extend(args)

        mgr = OnDemandDepthManager(
            exchange="bybit",
            market="linear",
            send_chunk=send_chunk,
            confirmed_topics=confirmed,
            settings=_manager_settings(tmp_path),
        )
        mgr.leases.acquire(symbol="BTCUSDT", session_id="a", lease_id="a")
        mgr.leases.acquire(symbol="BTCUSDT", session_id="b", lease_id="b")
        await mgr.tick(object())
        assert [c[0] for c in calls].count("subscribe") == 1

    asyncio.run(_run())


def test_grace_then_unsubscribe(tmp_path: Path):
    async def _run() -> None:
        calls: list[tuple[str, list[str]]] = []

        async def send_chunk(ws, op, args):
            calls.append((op, list(args)))

        mgr = OnDemandDepthManager(
            exchange="bybit",
            market="linear",
            send_chunk=send_chunk,
            confirmed_topics=["orderbook.1000.BTCUSDT"],
            settings=_manager_settings(tmp_path, lease_ttl_sec=0.01),
        )
        mgr.leases.acquire(symbol="BTCUSDT", session_id="a", lease_id="a")
        await mgr.tick(object())
        await mgr.handle_request(
            {"request_id": "rel", "operation": "release", "lease_id": "a", "depth": 1000}
        )
        await asyncio.sleep(0.02)
        await mgr.tick(object())
        assert ("unsubscribe", ["orderbook.1000.BTCUSDT"]) in calls

    asyncio.run(_run())


def test_stale_socket_removed(tmp_path: Path):
    sock_path = tmp_path / "ob1000.sock"
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.close()
    prepare_socket_path(sock_path)
    assert not sock_path.exists()


def test_ob1000_message_not_handled_when_no_lease():
    mgr = OnDemandDepthManager(
        exchange="bybit",
        market="linear",
        send_chunk=lambda *a, **k: asyncio.sleep(0),
        confirmed_topics=[],
        settings={
            "enabled": True,
            "max_active_topics": 4,
            "heartbeat_sec": 15,
            "lease_ttl_sec": 45,
            "socket_path": Path("/tmp/unused.sock"),
            "pilot_symbols": {"BTCUSDT", "DOGEUSDT"},
        },
    )
    payload = {
        "topic": "orderbook.1000.BTCUSDT",
        "type": "snapshot",
        "ts": 1_700_000_000_000,
        "data": {"s": "BTCUSDT", "b": [["1", "1"]], "a": [["2", "1"]], "u": 1, "seq": 1},
    }
    assert mgr.handle_message(payload, datetime.now(timezone.utc)) is False


def test_parallel_snapshot_under_lock(tmp_path: Path):
    mgr = OnDemandDepthManager(
        exchange="bybit",
        market="linear",
        send_chunk=lambda *a, **k: asyncio.sleep(0),
        confirmed_topics=[],
        settings=_manager_settings(tmp_path),
    )
    mgr.leases.acquire(symbol="BTCUSDT", session_id="tab", lease_id="tab")
    rt = mgr._get_runtime("BTCUSDT", 1000)
    rt.subscription_state = "live"
    rt.last_event_timestamp = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)
    rt.clock.ingest(
        "snapshot",
        int(rt.last_event_timestamp.timestamp() * 1000),
        {"s": "BTCUSDT", "b": [["99", "1"], ["98", "2"]], "a": [["101", "1"]], "u": 1, "seq": 1},
        generation=rt.clock.generation,
    )
    results: list[dict] = []

    def worker() -> None:
        results.append(mgr.build_snapshot("BTCUSDT"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r["best_bid"] == 99.0 for r in results if r.get("best_bid") is not None)

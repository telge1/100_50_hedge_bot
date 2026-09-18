"""Synthetic tests for Full-OB RO cache bridge (no live collector)."""

from __future__ import annotations

import asyncio
import os
import socket
import stat
import time
from dataclasses import dataclass
from pathlib import Path

import orjson
import pytest

from orderbook_analyse.orderbook_v2_live.full_book_state import ConsistentBookSnapshot, FullBookState
from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.checkpoints import (
    BookCheckpoint,
    CheckpointRing,
)
from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.client import (
    BridgeClientError,
    FullObCacheBridgeClient,
)
from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.config import (
    CacheBridgeSettings,
    load_cache_bridge_settings,
)
from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.dump import (
    build_payload_bytes,
    write_freeze_dump,
)
from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.freeze import (
    build_freeze_bundle,
    canonical_manifest_hash,
    filter_deltas_to_t0,
    replay_anchor_and_deltas,
    sha256_bytes,
)
from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.paths import (
    UnsafeDumpPath,
    resolve_dump_dir,
)
from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.protocol import (
    PROTOCOL_VERSION,
    STATUS_BOOK_ANCHOR_MISSING,
    STATUS_BRIDGE_DISABLED,
    STATUS_DATA_COMPLETE,
    STATUS_SEQUENCE_GAP,
    STATUS_SYMBOL_NOT_ALLOWED,
)
from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.service import FullObCacheBridge
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.ringbuffer import (
    BoundedRawRingBuffer,
)


@dataclass
class _Item:
    receive_time_ns: int
    payload: dict
    kind: str = "delta"
    approx_bytes: int = 100


def _delta(u: int, *, recv_ns: int, bid: float = 100.0, ask: float = 101.0, seq: int | None = None) -> _Item:
    return _Item(
        receive_time_ns=recv_ns,
        payload={
            "type": "delta",
            "ts": recv_ns // 1_000_000,
            "cts": recv_ns // 1_000_000,
            "data": {
                "s": "BTCUSDT",
                "b": [[str(bid), "1"]],
                "a": [[str(ask), "1"]],
                "u": u,
                "seq": seq if seq is not None else u * 10,
            },
            "local_receive_time_ns": recv_ns,
        },
    )


def _book_snap(u: int, *, recv_ns: int, bid: float = 100.0, ask: float = 101.0) -> ConsistentBookSnapshot:
    return ConsistentBookSnapshot(
        symbol="BTCUSDT",
        bids={bid: 1.0},
        asks={ask: 1.0},
        update_id=u,
        seq=u * 10,
        event_ts_ms=recv_ns // 1_000_000,
        cts_ms=recv_ns // 1_000_000,
        receive_time_ns=recv_ns,
        book_ready=True,
    )


def test_bridge_default_disabled(monkeypatch):
    monkeypatch.delenv("FULL_OB_CACHE_BRIDGE_ENABLED", raising=False)
    cfg = load_cache_bridge_settings()
    assert cfg.enabled is False


def test_socket_permissions(tmp_path):
    async def _run():
        from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.socket_server import (
            BridgeSocketServer,
        )

        path = tmp_path / "bridge.sock"

        def handler(req):
            return {"ok": True, "protocol_version": PROTOCOL_VERSION, "status": "DATA_COMPLETE"}

        srv = BridgeSocketServer(path, handler, mode=0o600)
        await srv.start()
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
            assert mode == 0o600
        finally:
            await srv.stop()

    asyncio.run(_run())


def test_symbol_allowlist_and_protocol(tmp_path):
    settings = CacheBridgeSettings(
        enabled=True,
        socket_path=tmp_path / "b.sock",
        dump_root=tmp_path / "dumps",
        symbols=frozenset({"BTCUSDT"}),
        min_request_interval_sec=0.0,
    )
    bridge = FullObCacheBridge(settings)
    bad = bridge.handle_request(
        {
            "protocol_version": PROTOCOL_VERSION,
            "operation": "freeze_pre_roll",
            "request_id": "r1",
            "symbol": "ETHUSDT",
            "requested_pre_roll_seconds": 60,
        }
    )
    assert bad["status"] == STATUS_SYMBOL_NOT_ALLOWED
    mismatch = bridge.handle_request(
        {"protocol_version": "other_v0", "operation": "status"}
    )
    assert mismatch["status"] == "PROTOCOL_MISMATCH"


def test_invalid_json_and_oversized_request(tmp_path):
    async def _run():
        from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.socket_server import (
            BridgeSocketServer,
        )

        path = tmp_path / "b.sock"
        srv = BridgeSocketServer(path, lambda r: {"ok": True}, max_request_bytes=64)
        await srv.start()

        def _client_call(payload: bytes) -> dict:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(2)
                s.connect(str(path))
                s.sendall(payload)
                return orjson.loads(s.recv(4096).split(b"\n")[0])

        try:
            # Sync client must not block the event loop — run in a thread.
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, _client_call, b"{not-json\n")
            assert resp["status"] == "INVALID_REQUEST"
            resp = await loop.run_in_executor(None, _client_call, b"x" * 200 + b"\n")
            assert resp["error"] == "request_too_large"
        finally:
            await srv.stop()

    asyncio.run(_run())

def test_parallel_request_busy(tmp_path):
    settings = CacheBridgeSettings(
        enabled=True,
        dump_root=tmp_path / "dumps",
        socket_path=tmp_path / "b.sock",
        min_request_interval_sec=0.0,
        max_concurrent_requests=1,
    )
    held = threading_event = __import__("threading").Event()
    release = __import__("threading").Event()

    class SlowBridge(FullObCacheBridge):
        def _freeze_locked(self, **kwargs):
            held.set()
            release.wait(timeout=2)
            return super()._freeze_locked(**kwargs)

    bridge = SlowBridge(settings)
    results = []

    def worker(rid):
        results.append(
            bridge.handle_request(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "operation": "freeze_pre_roll",
                    "request_id": rid,
                    "symbol": "BTCUSDT",
                    "requested_pre_roll_seconds": 60,
                }
            )
        )

    t1 = __import__("threading").Thread(target=worker, args=("a1",))
    t2 = __import__("threading").Thread(target=worker, args=("a2",))
    t1.start()
    assert held.wait(timeout=2)
    t2.start()
    t2.join(timeout=2)
    release.set()
    t1.join(timeout=2)
    assert any(r.get("status") in {"REQUEST_BUSY", "FREEZE_CONCURRENCY_LIMIT"} for r in results)


def test_missing_anchor_fail_closed():
    t0 = 1_000_000_000_000
    items = [_delta(2, recv_ns=t0 - 5_000_000_000), _delta(3, recv_ns=t0 - 1_000_000_000)]
    bundle = build_freeze_bundle(
        symbol="BTCUSDT",
        t0_ns=t0,
        requested_pre_roll_seconds=60,
        min_pre_roll_seconds=1,
        max_pre_roll_seconds=600,
        ring_items=items,
        overflow_count=0,
        reconnect_count=0,
        reconnect_in_window=False,
        gap_count_runtime=0,
        last_receive_ns=t0 - 1_000_000_000,
        stale_after_ms=15_000,
        anchor=None,
        t0_book_snap=None,
    )
    assert bundle.data_complete is False
    assert bundle.status == STATUS_BOOK_ANCHOR_MISSING


def test_complete_anchor_plus_deltas_and_atomic_cut():
    t0 = 2_000_000_000_000
    # Anchor at/before analysis_start (t0-40s); deltas through T0; late delta ignored.
    anchor = BookCheckpoint.from_consistent(_book_snap(10, recv_ns=t0 - 40_000_000_000))
    items = [
        _delta(11, recv_ns=t0 - 40_000_000_000, bid=100.1),
        _delta(12, recv_ns=t0 - 30_000_000_000, bid=100.2),
        _delta(13, recv_ns=t0 - 20_000_000_000, bid=100.3),
        _delta(14, recv_ns=t0 - 10_000_000_000, bid=100.4),
        _delta(15, recv_ns=t0 - 1_000_000_000, bid=100.5),
        _delta(16, recv_ns=t0 + 5_000_000_000, bid=100.6),  # after T0
    ]
    filtered = filter_deltas_to_t0(items, t0_ns=t0)
    assert all(d["_bridge_receive_time_ns"] <= t0 for d in filtered)
    assert len(filtered) == 5
    bundle = build_freeze_bundle(
        symbol="BTCUSDT",
        t0_ns=t0,
        requested_pre_roll_seconds=40,
        min_pre_roll_seconds=1,
        max_pre_roll_seconds=600,
        ring_items=items,
        overflow_count=0,
        reconnect_count=0,
        reconnect_in_window=False,
        gap_count_runtime=0,
        last_receive_ns=t0 - 1_000_000_000,
        stale_after_ms=15_000,
        anchor=anchor,
        t0_book_snap={"u": 15, "seq": 150},
    )
    assert bundle.data_complete is True
    assert bundle.status == STATUS_DATA_COMPLETE
    assert bundle.replay_ok is True
    assert bundle.last_u == 15
    assert all(d["_bridge_receive_time_ns"] <= t0 for d in bundle.deltas)


def test_sequence_gap_fail_closed():
    t0 = 3_000_000_000_000
    anchor = BookCheckpoint.from_consistent(_book_snap(10, recv_ns=t0 - 10_000_000_000))
    items = [
        _delta(11, recv_ns=t0 - 10_000_000_000),
        _delta(13, recv_ns=t0 - 1_000_000_000),  # gap: skipped 12
    ]
    bundle = build_freeze_bundle(
        symbol="BTCUSDT",
        t0_ns=t0,
        requested_pre_roll_seconds=10,
        min_pre_roll_seconds=1,
        max_pre_roll_seconds=600,
        ring_items=items,
        overflow_count=0,
        reconnect_count=0,
        reconnect_in_window=False,
        gap_count_runtime=0,
        last_receive_ns=t0 - 1_000_000_000,
        stale_after_ms=15_000,
        anchor=anchor,
        t0_book_snap=None,
    )
    assert bundle.data_complete is False
    assert bundle.status in {STATUS_SEQUENCE_GAP, "DELTA_CONTINUITY_GAP"}


def test_overflow_reconnect_stale_gates():
    t0 = 4_000_000_000_000
    anchor = BookCheckpoint.from_consistent(_book_snap(1, recv_ns=t0 - 10_000_000_000))
    items = [_delta(2, recv_ns=t0 - 1_000_000_000)]
    for overflow, reconnect, stale_ns, expect in [
        (1, False, t0 - 1_000_000_000, "BUFFER_OVERFLOW"),
        (0, True, t0 - 1_000_000_000, "RESYNC_CHECKPOINT_MISSING"),
        (0, False, t0 - 60_000_000_000, "DATA_STALE"),
    ]:
        bundle = build_freeze_bundle(
            symbol="BTCUSDT",
            t0_ns=t0,
            requested_pre_roll_seconds=10,
            min_pre_roll_seconds=1,
            max_pre_roll_seconds=600,
            ring_items=items,
            overflow_count=overflow,
            reconnect_count=1 if reconnect else 0,
            reconnect_in_window=reconnect,
            gap_count_runtime=0,
            last_receive_ns=stale_ns,
            stale_after_ms=15_000,
            anchor=anchor,
            t0_book_snap=None,
        )
        assert bundle.data_complete is False
        assert bundle.status == expect


def test_event_and_receive_time_separated():
    t0 = 5_000_000_000_000
    item = _delta(2, recv_ns=t0 - 1000)
    item.payload["ts"] = 111
    item.payload["cts"] = 222
    out = filter_deltas_to_t0([item], t0_ns=t0)[0]
    assert out["ts"] == 111
    assert out["cts"] == 222
    assert out["local_receive_time_ns"] == t0 - 1000
    assert out["_bridge_receive_time_ns"] == t0 - 1000


def test_payload_and_manifest_hash_and_tamper(tmp_path):
    t0 = 6_000_000_000_000
    anchor = BookCheckpoint.from_consistent(_book_snap(1, recv_ns=t0 - 5_000_000_000))
    items = [_delta(2, recv_ns=t0 - 1_000_000_000)]
    bundle = build_freeze_bundle(
        symbol="BTCUSDT",
        t0_ns=t0,
        requested_pre_roll_seconds=5,
        min_pre_roll_seconds=1,
        max_pre_roll_seconds=600,
        ring_items=items,
        overflow_count=0,
        reconnect_count=0,
        reconnect_in_window=False,
        gap_count_runtime=0,
        last_receive_ns=t0 - 1_000_000_000,
        stale_after_ms=15_000,
        anchor=anchor,
        t0_book_snap={"u": 2, "seq": 20},
    )
    dump = write_freeze_dump(
        dump_root=tmp_path / "dumps",
        request_id="hash1",
        forecast_id_seed="seed",
        symbol="BTCUSDT",
        collector_instance_id="c1",
        bundle=bundle,
        max_payload_bytes=10_000_000,
    )
    payload = (Path(dump["dump_dir"]) / "payload.json").read_bytes()
    manifest = orjson.loads((Path(dump["dump_dir"]) / "manifest.json").read_bytes())
    assert FullObCacheBridgeClient.verify_payload_hash(payload, manifest["payload_sha256"])
    assert FullObCacheBridgeClient.verify_manifest(manifest)
    tampered = bytearray(payload)
    tampered[-2] ^= 0x01
    assert not FullObCacheBridgeClient.verify_payload_hash(bytes(tampered), manifest["payload_sha256"])
    bad_man = dict(manifest)
    bad_man["message_count"] = 999
    assert not FullObCacheBridgeClient.verify_manifest(bad_man)


def test_no_overwrite_and_path_traversal(tmp_path):
    root = tmp_path / "dumps"
    root.mkdir()
    (root / "exists1").mkdir()
    t0 = 7_000_000_000_000
    anchor = BookCheckpoint.from_consistent(_book_snap(1, recv_ns=t0 - 5_000_000_000))
    bundle = build_freeze_bundle(
        symbol="BTCUSDT",
        t0_ns=t0,
        requested_pre_roll_seconds=5,
        min_pre_roll_seconds=1,
        max_pre_roll_seconds=600,
        ring_items=[_delta(2, recv_ns=t0 - 1_000_000_000)],
        overflow_count=0,
        reconnect_count=0,
        reconnect_in_window=False,
        gap_count_runtime=0,
        last_receive_ns=t0 - 1_000_000_000,
        stale_after_ms=15_000,
        anchor=anchor,
        t0_book_snap={"u": 2},
    )
    with pytest.raises(UnsafeDumpPath):
        write_freeze_dump(
            dump_root=root,
            request_id="exists1",
            forecast_id_seed="s",
            symbol="BTCUSDT",
            collector_instance_id="c",
            bundle=bundle,
            max_payload_bytes=10_000_000,
        )
    with pytest.raises(UnsafeDumpPath):
        resolve_dump_dir(root, "../escape")
    with pytest.raises(UnsafeDumpPath):
        resolve_dump_dir(root, "a/b")


def test_symlink_escape_blocked(tmp_path):
    root = tmp_path / "dumps"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "linkdir"
    link.symlink_to(outside)
    with pytest.raises(UnsafeDumpPath):
        resolve_dump_dir(root, "linkdir")


def test_btc_doge_isolation(tmp_path):
    settings = CacheBridgeSettings(
        enabled=True,
        dump_root=tmp_path / "dumps",
        socket_path=tmp_path / "b.sock",
        symbols=frozenset({"BTCUSDT", "DOGEUSDT"}),
        min_request_interval_sec=0.0,
        checkpoint_interval_sec=0.0,
    )
    rings = {
        "BTCUSDT": BoundedRawRingBuffer(window_sec=600, max_messages=1000, max_bytes=10_000_000),
        "DOGEUSDT": BoundedRawRingBuffer(window_sec=600, max_messages=1000, max_bytes=10_000_000),
    }
    t0_base = time.time_ns()
    # BTC continuum 1..3, DOGE continuum 100..102
    for i, u in enumerate((1, 2, 3)):
        rings["BTCUSDT"].append(
            _delta(u, recv_ns=t0_base - (3 - i) * 1_000_000_000).payload,
            receive_time_ns=t0_base - (3 - i) * 1_000_000_000,
        )
    for i, u in enumerate((100, 101, 102)):
        rings["DOGEUSDT"].append(
            _delta(u, recv_ns=t0_base - (3 - i) * 1_000_000_000).payload,
            receive_time_ns=t0_base - (3 - i) * 1_000_000_000,
        )
    books = {
        "BTCUSDT": _book_snap(3, recv_ns=t0_base - 1_000_000_000),
        "DOGEUSDT": ConsistentBookSnapshot(
            symbol="DOGEUSDT",
            bids={0.1: 1.0},
            asks={0.11: 1.0},
            update_id=102,
            seq=1020,
            event_ts_ms=1,
            cts_ms=1,
            receive_time_ns=t0_base - 1_000_000_000,
            book_ready=True,
        ),
    }
    bridge = FullObCacheBridge(
        settings,
        get_ring_snapshot=lambda s: rings[s].snapshot(),
        get_ring_meta=lambda s: {
            "message_count": len(rings[s]),
            "overflow_count": rings[s].overflow_count,
            "buffer_start_ns": rings[s].snapshot()[0].receive_time_ns,
            "buffer_end_ns": rings[s].snapshot()[-1].receive_time_ns,
        },
        get_book_snapshot=lambda s: books[s],
        get_runtime_meta=lambda s: {
            "gap_count": 0,
            "reconnect_count": 0,
            "last_receive_time_ns": t0_base - 1_000_000_000,
            "freshness_ms": 1,
            "event_ts_ms": 1,
        },
    )
    bridge.on_book_update("BTCUSDT", _book_snap(1, recv_ns=t0_base - 12_000_000_000))
    bridge.on_book_update(
        "DOGEUSDT",
        ConsistentBookSnapshot(
            symbol="DOGEUSDT",
            bids={0.1: 1.0},
            asks={0.11: 1.0},
            update_id=100,
            seq=1000,
            event_ts_ms=1,
            cts_ms=1,
            receive_time_ns=t0_base - 12_000_000_000,
            book_ready=True,
        ),
    )
    r_btc = bridge.handle_request(
        {
            "protocol_version": PROTOCOL_VERSION,
            "operation": "freeze_pre_roll",
            "request_id": "iso_btc",
            "symbol": "BTCUSDT",
            "requested_pre_roll_seconds": 3,
        }
    )
    r_doge = bridge.handle_request(
        {
            "protocol_version": PROTOCOL_VERSION,
            "operation": "freeze_pre_roll",
            "request_id": "iso_doge",
            "symbol": "DOGEUSDT",
            "requested_pre_roll_seconds": 3,
        }
    )
    assert r_btc.get("ok") and r_doge.get("ok")
    assert r_btc["symbol"] == "BTCUSDT"
    assert r_doge["symbol"] == "DOGEUSDT"
    assert r_btc["dump_dir"] != r_doge["dump_dir"]


def test_no_ob200_fallback_in_protocol():
    # Contract surface must never mention OB200 fallback statuses.
    from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge import protocol as p

    joined = " ".join(sorted(p.KNOWN_STATUS_CODES))
    assert "OB200" not in joined
    assert "FALLBACK" not in joined


def test_disabled_handle_request():
    bridge = FullObCacheBridge(CacheBridgeSettings(enabled=False))
    resp = bridge.handle_request({"operation": "status"})
    assert resp["status"] == STATUS_BRIDGE_DISABLED


def test_deterministic_payload(tmp_path):
    t0 = 8_000_000_000_000
    anchor = BookCheckpoint.from_consistent(_book_snap(5, recv_ns=t0 - 10_000_000_000))
    items = [_delta(6, recv_ns=t0 - 2_000_000_000), _delta(7, recv_ns=t0 - 1_000_000_000)]
    b1 = build_freeze_bundle(
        symbol="BTCUSDT",
        t0_ns=t0,
        requested_pre_roll_seconds=10,
        min_pre_roll_seconds=1,
        max_pre_roll_seconds=600,
        ring_items=items,
        overflow_count=0,
        reconnect_count=0,
        reconnect_in_window=False,
        gap_count_runtime=0,
        last_receive_ns=t0 - 1_000_000_000,
        stale_after_ms=15_000,
        anchor=anchor,
        t0_book_snap={"u": 7, "seq": 70},
    )
    b2 = build_freeze_bundle(
        symbol="BTCUSDT",
        t0_ns=t0,
        requested_pre_roll_seconds=10,
        min_pre_roll_seconds=1,
        max_pre_roll_seconds=600,
        ring_items=items,
        overflow_count=0,
        reconnect_count=0,
        reconnect_in_window=False,
        gap_count_runtime=0,
        last_receive_ns=t0 - 1_000_000_000,
        stale_after_ms=15_000,
        anchor=anchor,
        t0_book_snap={"u": 7, "seq": 70},
    )
    assert build_payload_bytes(bundle=b1, symbol="BTCUSDT") == build_payload_bytes(
        bundle=b2, symbol="BTCUSDT"
    )


def test_client_timeout(tmp_path):
    async def _run():
        from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.socket_server import (
            BridgeSocketServer,
        )

        path = tmp_path / "slow.sock"
        started = __import__("threading").Event()

        def handler(req):
            started.set()
            time.sleep(2.0)
            return {"ok": True, "protocol_version": PROTOCOL_VERSION}

        srv = BridgeSocketServer(path, handler)
        await srv.start()
        loop = asyncio.get_running_loop()

        def _call():
            client = FullObCacheBridgeClient(path, timeout_sec=0.2)
            with pytest.raises(Exception):
                client.status()

        try:
            await loop.run_in_executor(None, _call)
        finally:
            await srv.stop()

    asyncio.run(_run())


def test_offline_integration_replay_matches_expected(tmp_path):
    """Anchor + multi-minute deltas → freeze → client verify → replay to T0 book."""
    settings = CacheBridgeSettings(
        enabled=True,
        dump_root=tmp_path / "dumps",
        socket_path=tmp_path / "bridge.sock",
        min_request_interval_sec=0.0,
        checkpoint_interval_sec=0.0,
        min_pre_roll_seconds=1.0,
    )
    ring = BoundedRawRingBuffer(window_sec=600, max_messages=10_000, max_bytes=50_000_000)
    base = time.time_ns() - 180_000_000_000  # ~3 minutes ago
    # Seed book u=1
    book = FullBookState(symbol="BTCUSDT")
    book.apply_snapshot(
        bids=[[100.0, 1.0]],
        asks=[[101.0, 1.0]],
        u=1,
        seq=10,
        ts_ms=1,
        receive_time_ns=base,
    )
    # Apply and buffer deltas u=2..90 (~3 minutes at ~2s steps for test speed use 1s)
    for i in range(2, 91):
        recv = base + (i - 1) * 2_000_000_000
        bid = 100.0 + i * 0.01
        out = book.apply_delta(
            bids=[[bid, 1.0]],
            asks=[[101.0 + i * 0.01, 1.0]],
            u=i,
            seq=i * 10,
            ts_ms=recv // 1_000_000,
            receive_time_ns=recv,
        )
        assert out.value == "applied"
        ring.append(
            _delta(i, recv_ns=recv, bid=bid, ask=101.0 + i * 0.01).payload,
            receive_time_ns=recv,
        )
    expected_u = book.update_id
    expected_bid = book.best_bid()

    def get_book(_s):
        return book.copy_consistent_snapshot()

    bridge = FullObCacheBridge(
        settings,
        collector_instance_id="offline-int",
        get_ring_snapshot=lambda s: ring.snapshot(),
        get_ring_meta=lambda s: {
            "message_count": len(ring),
            "overflow_count": ring.overflow_count,
            "buffer_start_ns": ring.snapshot()[0].receive_time_ns,
            "buffer_end_ns": ring.snapshot()[-1].receive_time_ns,
        },
        get_book_snapshot=get_book,
        get_runtime_meta=lambda s: {
            "gap_count": 0,
            "reconnect_count": 0,
            "last_receive_time_ns": ring.snapshot()[-1].receive_time_ns,
            "freshness_ms": 10,
            "event_ts_ms": 1,
        },
    )
    # Store early checkpoint at u=1
    bridge.on_book_update(
        "BTCUSDT",
        ConsistentBookSnapshot(
            symbol="BTCUSDT",
            bids={100.0: 1.0},
            asks={101.0: 1.0},
            update_id=1,
            seq=10,
            event_ts_ms=1,
            cts_ms=1,
            receive_time_ns=base - 30_000_000_000,
            book_ready=True,
        ),
    )

    async def _serve_and_freeze():
        await bridge.start()
        loop = asyncio.get_running_loop()

        def _client_work():
            client = FullObCacheBridgeClient(settings.socket_path, timeout_sec=5)
            st = client.status()
            assert st["bridge_enabled"] is True
            resp = client.freeze_pre_roll(
                request_id="offline_ok",
                symbol="BTCUSDT",
                requested_pre_roll_seconds=150,
            )
            assert resp["ok"] is True
            assert resp["data_complete"] is True
            dump = Path(resp["dump_dir"])
            payload = (dump / "payload.json").read_bytes()
            manifest = orjson.loads((dump / "manifest.json").read_bytes())
            assert client.verify_payload_hash(payload, manifest["payload_sha256"])
            assert client.verify_manifest(manifest)
            obj = orjson.loads(payload)
            assert all(
                int(d.get("_bridge_receive_time_ns") or 0) <= int(obj["t0_ns"])
                for d in obj["deltas"]
            )
            ck = BookCheckpoint.from_dict(obj["anchor"])
            replay = replay_anchor_and_deltas(
                symbol="BTCUSDT", anchor=ck, deltas=obj["deltas"]
            )
            assert replay["ok"] is True
            assert replay["final_u"] == expected_u
            assert abs(float(replay["best_bid"]) - float(expected_bid)) < 1e-9
            return {"resp": resp, "replay": replay}

        try:
            return await loop.run_in_executor(None, _client_work)
        finally:
            await bridge.stop()

    result = asyncio.run(_serve_and_freeze())
    assert result["resp"]["status"] == STATUS_DATA_COMPLETE


def test_offline_negative_sequence_gap(tmp_path):
    t0 = time.time_ns()
    anchor = BookCheckpoint.from_consistent(_book_snap(1, recv_ns=t0 - 10_000_000_000))
    items = [_delta(2, recv_ns=t0 - 5_000_000_000), _delta(4, recv_ns=t0 - 1_000_000_000)]
    bundle = build_freeze_bundle(
        symbol="BTCUSDT",
        t0_ns=t0,
        requested_pre_roll_seconds=10,
        min_pre_roll_seconds=1,
        max_pre_roll_seconds=600,
        ring_items=items,
        overflow_count=0,
        reconnect_count=0,
        reconnect_in_window=False,
        gap_count_runtime=0,
        last_receive_ns=t0 - 1_000_000_000,
        stale_after_ms=15_000,
        anchor=anchor,
        t0_book_snap=None,
    )
    assert bundle.data_complete is False
    assert bundle.status in {STATUS_SEQUENCE_GAP, "DELTA_CONTINUITY_GAP"}

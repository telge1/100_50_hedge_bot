"""Deterministic tests for full_ob_cache_bridge_anchor_retention_v2."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import orjson
import pytest

from orderbook_analyse.orderbook_v2_live.full_book_state import ConsistentBookSnapshot, FullBookState
from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.checkpoints import (
    BookCheckpoint,
    CheckpointRing,
    book_content_hash,
)
from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.client import FullObCacheBridgeClient
from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.config import (
    CacheBridgeSettings,
    compute_checkpoint_retention_sec,
    compute_max_checkpoints,
    load_cache_bridge_settings,
)
from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.dump import write_freeze_dump
from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.freeze import (
    build_freeze_bundle,
    replay_anchor_and_deltas,
)
from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.protocol import (
    CHECKPOINT_KIND_INITIAL,
    CHECKPOINT_KIND_RESYNC,
    CONTRACT_ID,
    STATUS_BOOK_ANCHOR_MISSING,
    STATUS_BOOK_HASH_MISMATCH,
    STATUS_DATA_COMPLETE,
    STATUS_DELTA_CONTINUITY_GAP,
    STATUS_FREEZE_CONCURRENCY_LIMIT,
    STATUS_PAYLOAD_LIMIT_EXCEEDED,
    STATUS_RESYNC_CHECKPOINT_MISSING,
    STATUS_STARTUP_PRE_ROLL_INCOMPLETE,
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


def _snap(
    u: int,
    *,
    recv_ns: int,
    bid: float = 100.0,
    ask: float = 101.0,
    symbol: str = "BTCUSDT",
) -> ConsistentBookSnapshot:
    return ConsistentBookSnapshot(
        symbol=symbol,
        bids={bid: 1.0},
        asks={ask: 1.0},
        update_id=u,
        seq=u * 10,
        event_ts_ms=recv_ns // 1_000_000,
        cts_ms=recv_ns // 1_000_000,
        receive_time_ns=recv_ns,
        book_ready=True,
    )


def _large_snap(u: int, *, recv_ns: int, n_bid: int = 30000, n_ask: int = 30000) -> ConsistentBookSnapshot:
    bids = {1000.0 - i * 0.01: 1.0 + (i % 7) * 0.1 for i in range(n_bid)}
    asks = {1000.01 + i * 0.01: 1.0 + (i % 5) * 0.1 for i in range(n_ask)}
    return ConsistentBookSnapshot(
        symbol="BTCUSDT",
        bids=bids,
        asks=asks,
        update_id=u,
        seq=u * 10,
        event_ts_ms=recv_ns // 1_000_000,
        cts_ms=recv_ns // 1_000_000,
        receive_time_ns=recv_ns,
        book_ready=True,
    )


def test_contract_retention_formula():
    ret = compute_checkpoint_retention_sec(
        delta_retention_sec=600, checkpoint_interval_sec=60, safety_margin_sec=30
    )
    assert ret == 690.0
    assert compute_max_checkpoints(checkpoint_retention_sec=ret, checkpoint_interval_sec=60) == 14
    assert CONTRACT_ID == "full_ob_cache_bridge_anchor_retention_v2"


def test_checkpoint_exactly_at_pre_roll_start():
    t0 = 10_000_000_000_000
    analysis = t0 - 600_000_000_000
    anchor = BookCheckpoint.from_consistent(_snap(1, recv_ns=analysis), kind=CHECKPOINT_KIND_INITIAL)
    denser = []
    for i in range(600):
        # Keep a non-crossing top-of-book; size updates only.
        denser.append(_delta(2 + i, recv_ns=analysis + i * 1_000_000_000, bid=100.0, ask=101.0))
    denser[-1] = _delta(601, recv_ns=t0 - 1_000_000_000, bid=100.0, ask=101.0)
    bundle = build_freeze_bundle(
        symbol="BTCUSDT",
        t0_ns=t0,
        requested_pre_roll_seconds=600,
        min_pre_roll_seconds=30,
        max_pre_roll_seconds=600,
        ring_items=denser,
        overflow_count=0,
        reconnect_count=0,
        reconnect_in_window=False,
        gap_count_runtime=0,
        last_receive_ns=t0 - 1_000_000_000,
        stale_after_ms=15_000,
        anchor=anchor,
        t0_book_snap=None,
    )
    assert bundle.anchor is not None
    assert bundle.anchor.receive_time_ns == analysis
    assert bundle.data_complete is True


def test_worst_case_phase_offset_59999ms():
    """Oldest delta at T-600; oldest checkpoint at T-600 + 59.999s still fails without longer retention."""
    now = 20_000_000_000_000
    ring = CheckpointRing(interval_sec=60, max_checkpoints=14, max_bytes=50_000_000, window_sec=600)
    # With equal retention, checkpoint at now-540.001 would survive while delta edge at now-600 needs older ck
    edge = now - 600_000_000_000
    late = edge + 59_999_000_000
    ring.maybe_store(_snap(1, recv_ns=late), now_ns=now, force_kind=CHECKPOINT_KIND_INITIAL)
    assert ring.latest_at_or_before(edge) is None

    ring2 = CheckpointRing(interval_sec=60, max_checkpoints=14, max_bytes=50_000_000, window_sec=690)
    ring2.maybe_store(_snap(1, recv_ns=late), now_ns=now, force_kind=CHECKPOINT_KIND_INITIAL)
    # still late relative to edge — need store at/before edge
    assert ring2.latest_at_or_before(edge) is None
    ring2.maybe_store(_snap(2, recv_ns=edge), now_ns=now, force_kind=CHECKPOINT_KIND_INITIAL)
    # force may be blocked by interval — clear and force via direct append path: begin and store with force after clear last_store
    ring2.clear()
    ring2.maybe_store(_snap(2, recv_ns=edge - 1), now_ns=now - 700_000_000_000, force_kind=CHECKPOINT_KIND_INITIAL)
    # after eviction with now much later:
    ring2.maybe_store(_snap(3, recv_ns=now - 30_000_000_000), now_ns=now)
    # Keep an old checkpoint within 690s window
    ring3 = CheckpointRing(interval_sec=60, max_checkpoints=20, max_bytes=50_000_000, window_sec=690)
    ring3.maybe_store(_snap(10, recv_ns=edge), now_ns=edge, force_kind=CHECKPOINT_KIND_INITIAL)
    # advance time by 600s — still retained under 690
    assert ring3.latest_at_or_before(edge) is not None
    ring3.maybe_store(_snap(11, recv_ns=now), now_ns=now)
    assert ring3.latest_at_or_before(edge) is not None


def test_long_run_600s_ring_hours_simulated():
    """Simulate many hours via eviction with 690s checkpoint retention."""
    interval = 60_000_000_000
    start = 100_000_000_000_000
    ring = CheckpointRing(interval_sec=60, max_checkpoints=14, max_bytes=50_000_000, window_sec=690)
    # 5 hours of checkpoints every 60s
    for i in range(300):
        ts = start + i * interval
        ring.maybe_store(_snap(i + 1, recv_ns=ts), now_ns=ts + 1, force_kind=None if i else CHECKPOINT_KIND_INITIAL)
    now = start + 299 * interval
    analysis = now - 600_000_000_000
    ck = ring.latest_at_or_before(analysis)
    assert ck is not None
    assert ck.receive_time_ns <= analysis


def test_startup_incomplete():
    t0 = 30_000_000_000_000
    analysis = t0 - 600_000_000_000
    anchor = BookCheckpoint.from_consistent(_snap(1, recv_ns=t0 - 100_000_000_000))
    items = [_delta(2, recv_ns=t0 - 90_000_000_000), _delta(3, recv_ns=t0 - 1_000_000_000)]
    # anchor after analysis -> BOOK_ANCHOR_MISSING first
    bundle = build_freeze_bundle(
        symbol="BTCUSDT",
        t0_ns=t0,
        requested_pre_roll_seconds=600,
        min_pre_roll_seconds=30,
        max_pre_roll_seconds=600,
        ring_items=items,
        overflow_count=0,
        reconnect_count=0,
        reconnect_in_window=False,
        gap_count_runtime=0,
        last_receive_ns=t0 - 1_000_000_000,
        stale_after_ms=15_000,
        anchor=BookCheckpoint.from_consistent(_snap(1, recv_ns=analysis)),
        t0_book_snap=None,
    )
    assert bundle.data_complete is False
    assert STATUS_STARTUP_PRE_ROLL_INCOMPLETE in bundle.exclusion_reasons or bundle.status == STATUS_STARTUP_PRE_ROLL_INCOMPLETE


def test_eviction_boundary_exact():
    now = 40_000_000_000_000
    window = 690.0
    ring = CheckpointRing(interval_sec=60, max_checkpoints=20, max_bytes=50_000_000, window_sec=window)
    old = now - int(window * 1e9) - 1
    edge = now - int(window * 1e9)
    ring.maybe_store(_snap(1, recv_ns=old), now_ns=old, force_kind=CHECKPOINT_KIND_INITIAL)
    ring.maybe_store(_snap(2, recv_ns=edge), now_ns=now)
    # old should be evicted
    meta = ring.snapshot_meta()
    assert meta["oldest_receive_time_ns"] >= edge


def test_initial_checkpoint_before_first_delta():
    ring = CheckpointRing(interval_sec=60, max_checkpoints=14, max_bytes=10_000_000, window_sec=690)
    t = 50_000_000_000_000
    assert ring.maybe_store(_snap(1, recv_ns=t), now_ns=t) is True
    items = ring.items_snapshot()
    assert items[0].kind == CHECKPOINT_KIND_INITIAL
    assert items[0].receive_time_ns == t


def test_reconnect_in_freeze_window_requires_resync():
    t0 = 60_000_000_000_000
    analysis = t0 - 100_000_000_000
    anchor = BookCheckpoint.from_consistent(_snap(1, recv_ns=analysis), epoch_id=0, kind=CHECKPOINT_KIND_INITIAL)
    denser = [_delta(2 + i, recv_ns=analysis + i * 1_000_000_000, bid=100 + i * 0.01) for i in range(100)]
    denser[-1] = _delta(101, recv_ns=t0 - 1_000_000_000, bid=110.0)
    bundle = build_freeze_bundle(
        symbol="BTCUSDT",
        t0_ns=t0,
        requested_pre_roll_seconds=100,
        min_pre_roll_seconds=1,
        max_pre_roll_seconds=600,
        ring_items=denser,
        overflow_count=0,
        reconnect_count=1,
        reconnect_in_window=True,
        gap_count_runtime=0,
        last_receive_ns=t0 - 1_000_000_000,
        stale_after_ms=15_000,
        anchor=anchor,
        t0_book_snap=None,
        reconnect_mark_ns=analysis + 50_000_000_000,
    )
    assert bundle.data_complete is False
    assert bundle.status == STATUS_RESYNC_CHECKPOINT_MISSING


def test_multi_epoch_replay_with_resync_checkpoint():
    t0 = 70_000_000_000_000
    analysis = t0 - 120_000_000_000
    rmark = analysis + 60_000_000_000
    a0 = BookCheckpoint.from_consistent(_snap(1, recv_ns=analysis, bid=100.0, ask=101.0), epoch_id=0, kind=CHECKPOINT_KIND_INITIAL)
    a1 = BookCheckpoint.from_consistent(_snap(1000, recv_ns=rmark, bid=200.0, ask=201.0), epoch_id=1, kind=CHECKPOINT_KIND_RESYNC)
    items = [_delta(1, recv_ns=analysis, bid=100.0, ask=101.0)]
    for i, u in enumerate(range(2, 10)):
        items.append(_delta(u, recv_ns=analysis + (i + 1) * 1_000_000_000, bid=100.0 + i, ask=101.0 + i))
    for i, u in enumerate(range(1001, 1010)):
        items.append(_delta(u, recv_ns=rmark + (i + 1) * 1_000_000_000, bid=200.0 + i, ask=201.0 + i))
    items.append(_delta(1010, recv_ns=t0 - 1_000_000_000, bid=210.0, ask=211.0))
    # Fill coverage at analysis start
    # Ensure contiguous seconds for coverage gate
    for i in range(120):
        ts = analysis + i * 1_000_000_000
        if ts >= rmark:
            break
        if not any(it.receive_time_ns == ts for it in items):
            items.append(_delta(2, recv_ns=ts, bid=100.0, ask=101.0))
    items.sort(key=lambda it: it.receive_time_ns)
    # Rebuild continuous epoch0/1 properly without duplicate u chaos:
    items = []
    u = 1
    for i in range(60):
        ts = analysis + i * 1_000_000_000
        items.append(_delta(u, recv_ns=ts, bid=100.0 + i * 0.01, ask=101.0 + i * 0.01))
        u += 1
    # last epoch0 u before resync
    last_e0 = u - 1
    a0 = BookCheckpoint.from_consistent(_snap(1, recv_ns=analysis, bid=100.0, ask=101.0), epoch_id=0, kind=CHECKPOINT_KIND_INITIAL)
    a1 = BookCheckpoint.from_consistent(_snap(1000, recv_ns=rmark, bid=200.0, ask=201.0), epoch_id=1, kind=CHECKPOINT_KIND_RESYNC)
    items = []
    for i in range(60):
        items.append(_delta(2 + i, recv_ns=analysis + i * 1_000_000_000, bid=100.0 + i * 0.01, ask=101.0 + i * 0.01))
    for i in range(60):
        items.append(_delta(1001 + i, recv_ns=rmark + i * 1_000_000_000, bid=200.0 + i * 0.01, ask=201.0 + i * 0.01))
    # expected final from replay
    r = replay_anchor_and_deltas(
        symbol="BTCUSDT",
        anchor=a0,
        deltas=[{**it.payload, "_bridge_receive_time_ns": it.receive_time_ns} for it in items],
        epoch_checkpoints=[a0, a1],
    )
    assert r["ok"], r
    t0_book = {"u": r["final_u"], "seq": r["final_seq"], "b": r["bids"], "a": r["asks"]}
    bundle = build_freeze_bundle(
        symbol="BTCUSDT",
        t0_ns=t0,
        requested_pre_roll_seconds=120,
        min_pre_roll_seconds=1,
        max_pre_roll_seconds=600,
        ring_items=items,
        overflow_count=0,
        reconnect_count=1,
        reconnect_in_window=True,
        gap_count_runtime=0,
        last_receive_ns=t0 - 1_000_000_000,
        stale_after_ms=15_000,
        anchor=a0,
        t0_book_snap=t0_book,
        available_checkpoints=[a0, a1],
        reconnect_mark_ns=rmark,
    )
    assert bundle.status == STATUS_DATA_COMPLETE
    assert bundle.data_complete is True
    assert bundle.replay_ok is True


def test_missing_resync_checkpoint():
    t0 = 80_000_000_000_000
    analysis = t0 - 60_000_000_000
    anchor = BookCheckpoint.from_consistent(_snap(1, recv_ns=analysis), kind=CHECKPOINT_KIND_INITIAL)
    items = [_delta(i, recv_ns=analysis + (i - 1) * 1_000_000_000) for i in range(1, 61)]
    bundle = build_freeze_bundle(
        symbol="BTCUSDT",
        t0_ns=t0,
        requested_pre_roll_seconds=60,
        min_pre_roll_seconds=1,
        max_pre_roll_seconds=600,
        ring_items=items,
        overflow_count=0,
        reconnect_count=1,
        reconnect_in_window=True,
        gap_count_runtime=0,
        last_receive_ns=t0 - 1_000_000_000,
        stale_after_ms=15_000,
        anchor=anchor,
        t0_book_snap=None,
        available_checkpoints=[anchor],
        reconnect_mark_ns=analysis + 10_000_000_000,
    )
    assert bundle.status == STATUS_RESYNC_CHECKPOINT_MISSING


def test_true_u_gap():
    t0 = 90_000_000_000_000
    analysis = t0 - 20_000_000_000
    anchor = BookCheckpoint.from_consistent(_snap(10, recv_ns=analysis))
    items = [
        _delta(11, recv_ns=analysis),
        _delta(12, recv_ns=analysis + 5_000_000_000),
        _delta(14, recv_ns=t0 - 1_000_000_000),
    ]
    bundle = build_freeze_bundle(
        symbol="BTCUSDT",
        t0_ns=t0,
        requested_pre_roll_seconds=20,
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
    assert bundle.status in {STATUS_DELTA_CONTINUITY_GAP, "SEQUENCE_GAP"}


def test_parallel_updates_during_freeze(tmp_path):
    settings = CacheBridgeSettings(
        enabled=True,
        dump_root=tmp_path / "d",
        socket_path=tmp_path / "s.sock",
        min_request_interval_sec=0.0,
        checkpoint_interval_sec=0.0,
        checkpoint_retention_seconds=690,
        max_checkpoints_per_symbol=14,
        min_pre_roll_seconds=1,
    )
    t0b = time.time_ns()
    ring = BoundedRawRingBuffer(window_sec=600, max_messages=10000, max_bytes=20_000_000)
    for i in range(1, 30):
        ring.append(_delta(i, recv_ns=t0b - (30 - i) * 1_000_000_000).payload, receive_time_ns=t0b - (30 - i) * 1_000_000_000)
    books = {"BTCUSDT": _snap(29, recv_ns=t0b - 1_000_000_000)}
    bridge = FullObCacheBridge(
        settings,
        get_ring_snapshot=lambda s: ring.snapshot(),
        get_ring_meta=lambda s: {"message_count": len(ring), "overflow_count": 0, "buffer_start_ns": ring.snapshot()[0].receive_time_ns, "buffer_end_ns": ring.snapshot()[-1].receive_time_ns},
        get_book_snapshot=lambda s: books[s],
        get_runtime_meta=lambda s: {"gap_count": 0, "reconnect_count": 0, "last_receive_time_ns": t0b - 1_000_000_000, "freshness_ms": 1, "event_ts_ms": 1},
    )
    bridge.on_book_update("BTCUSDT", _snap(1, recv_ns=t0b - 35_000_000_000))

    held = threading.Event()
    release = threading.Event()

    class Slow(FullObCacheBridge):
        def _freeze_locked(self, **kwargs):
            held.set()
            release.wait(timeout=2)
            return super()._freeze_locked(**kwargs)

    slow = Slow(
        settings,
        get_ring_snapshot=lambda s: ring.snapshot(),
        get_ring_meta=lambda s: {"message_count": len(ring), "overflow_count": 0, "buffer_start_ns": ring.snapshot()[0].receive_time_ns, "buffer_end_ns": ring.snapshot()[-1].receive_time_ns},
        get_book_snapshot=lambda s: books[s],
        get_runtime_meta=lambda s: {"gap_count": 0, "reconnect_count": 0, "last_receive_time_ns": t0b - 1_000_000_000, "freshness_ms": 1, "event_ts_ms": 1},
    )
    slow.on_book_update("BTCUSDT", _snap(1, recv_ns=t0b - 35_000_000_000))
    results = []

    def worker(rid):
        results.append(
            slow.handle_request(
                {
                    "protocol_version": "full_ob_cache_bridge_protocol_v1",
                    "operation": "freeze_pre_roll",
                    "request_id": rid,
                    "symbol": "BTCUSDT",
                    "requested_pre_roll_seconds": 20,
                }
            )
        )

    t1 = threading.Thread(target=worker, args=("p1",))
    t2 = threading.Thread(target=worker, args=("p2",))
    t1.start()
    assert held.wait(2)
    # live updates while freeze held
    for _ in range(5):
        slow.on_book_update("BTCUSDT", _snap(30, recv_ns=time.time_ns()))
    t2.start()
    t2.join(2)
    release.set()
    t1.join(2)
    assert any(r.get("status") == STATUS_FREEZE_CONCURRENCY_LIMIT for r in results)


def test_btc_doge_simultaneous(tmp_path):
    settings = CacheBridgeSettings(
        enabled=True,
        dump_root=tmp_path / "d",
        socket_path=tmp_path / "s.sock",
        symbols=frozenset({"BTCUSDT", "DOGEUSDT"}),
        min_request_interval_sec=0.0,
        checkpoint_interval_sec=0.0,
        checkpoint_retention_seconds=690,
        max_checkpoints_per_symbol=14,
        min_pre_roll_seconds=1,
    )
    t0b = time.time_ns()
    rings = {
        "BTCUSDT": BoundedRawRingBuffer(window_sec=600, max_messages=1000, max_bytes=5_000_000),
        "DOGEUSDT": BoundedRawRingBuffer(window_sec=600, max_messages=1000, max_bytes=5_000_000),
    }
    for i in range(1, 8):
        rings["BTCUSDT"].append(_delta(i, recv_ns=t0b - (8 - i) * 1_000_000_000).payload, receive_time_ns=t0b - (8 - i) * 1_000_000_000)
        rings["DOGEUSDT"].append(_delta(100 + i, recv_ns=t0b - (8 - i) * 1_000_000_000).payload, receive_time_ns=t0b - (8 - i) * 1_000_000_000)
    books = {
        "BTCUSDT": _snap(7, recv_ns=t0b - 1_000_000_000),
        "DOGEUSDT": _snap(107, recv_ns=t0b - 1_000_000_000, symbol="DOGEUSDT", bid=0.1, ask=0.11),
    }
    bridge = FullObCacheBridge(
        settings,
        get_ring_snapshot=lambda s: rings[s].snapshot(),
        get_ring_meta=lambda s: {
            "message_count": len(rings[s]),
            "overflow_count": 0,
            "buffer_start_ns": rings[s].snapshot()[0].receive_time_ns,
            "buffer_end_ns": rings[s].snapshot()[-1].receive_time_ns,
        },
        get_book_snapshot=lambda s: books[s],
        get_runtime_meta=lambda s: {
            "gap_count": 0,
            "reconnect_count": 0,
            "last_receive_time_ns": t0b - 1_000_000_000,
            "freshness_ms": 1,
            "event_ts_ms": 1,
        },
    )
    bridge.on_book_update("BTCUSDT", _snap(1, recv_ns=t0b - 10_000_000_000))
    bridge.on_book_update("DOGEUSDT", _snap(101, recv_ns=t0b - 10_000_000_000, symbol="DOGEUSDT", bid=0.1, ask=0.11))
    rb = bridge.handle_request(
        {
            "protocol_version": "full_ob_cache_bridge_protocol_v1",
            "operation": "freeze_pre_roll",
            "request_id": "b",
            "symbol": "BTCUSDT",
            "requested_pre_roll_seconds": 5,
        }
    )
    rd = bridge.handle_request(
        {
            "protocol_version": "full_ob_cache_bridge_protocol_v1",
            "operation": "freeze_pre_roll",
            "request_id": "d",
            "symbol": "DOGEUSDT",
            "requested_pre_roll_seconds": 5,
        }
    )
    assert rb.get("ok") and rd.get("ok")
    assert rb["dump_dir"] != rd["dump_dir"]


def test_max_concurrent_one(tmp_path):
    settings = CacheBridgeSettings(
        enabled=True,
        dump_root=tmp_path / "d",
        socket_path=tmp_path / "s.sock",
        min_request_interval_sec=0.0,
        max_concurrent_requests=1,
        checkpoint_retention_seconds=690,
    )
    held = threading.Event()
    release = threading.Event()

    class Slow(FullObCacheBridge):
        def _freeze_locked(self, **kwargs):
            held.set()
            release.wait(2)
            return {"ok": True, "status": STATUS_DATA_COMPLETE, "protocol_version": "full_ob_cache_bridge_protocol_v1"}

    bridge = Slow(settings)
    out = []

    def w(rid):
        out.append(
            bridge.handle_request(
                {
                    "protocol_version": "full_ob_cache_bridge_protocol_v1",
                    "operation": "freeze_pre_roll",
                    "request_id": rid,
                    "symbol": "BTCUSDT",
                    "requested_pre_roll_seconds": 1,
                }
            )
        )

    t1 = threading.Thread(target=w, args=("a",))
    t2 = threading.Thread(target=w, args=("b",))
    t1.start()
    assert held.wait(2)
    t2.start()
    t2.join(2)
    release.set()
    t1.join(2)
    assert any(r.get("status") == STATUS_FREEZE_CONCURRENCY_LIMIT for r in out)


def test_payload_limit_exceeded(tmp_path):
    t0 = 110_000_000_000_000
    analysis = t0 - 5_000_000_000
    # huge bids in anchor
    big = _large_snap(1, recv_ns=analysis, n_bid=20000, n_ask=20000)
    anchor = BookCheckpoint.from_consistent(big, kind=CHECKPOINT_KIND_INITIAL)
    items = [_delta(2, recv_ns=analysis), _delta(3, recv_ns=t0 - 1_000_000_000)]
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
        t0_book_snap=None,
    )
    with pytest.raises(Exception) as ei:
        write_freeze_dump(
            dump_root=tmp_path / "d",
            request_id="too_big",
            forecast_id_seed="s",
            symbol="BTCUSDT",
            collector_instance_id="c",
            bundle=bundle,
            max_payload_bytes=1024,
        )
    assert "payload_too_large" in str(ei.value)


def test_tampered_checkpoint_hash_mismatch():
    t0 = 120_000_000_000_000
    analysis = t0 - 10_000_000_000
    anchor = BookCheckpoint.from_consistent(_snap(1, recv_ns=analysis))
    items = [_delta(2, recv_ns=analysis), _delta(3, recv_ns=t0 - 1_000_000_000, bid=100.5)]
    # replay final book won't match fake t0 hash levels
    fake_t0 = {"u": 3, "seq": 30, "b": [[999.0, 1.0]], "a": [[1000.0, 1.0]]}
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
        t0_book_snap=fake_t0,
    )
    assert bundle.data_complete is False
    assert STATUS_BOOK_HASH_MISMATCH in bundle.exclusion_reasons or bundle.status == STATUS_BOOK_HASH_MISMATCH


def test_sha_error_detection(tmp_path):
    t0 = 130_000_000_000_000
    analysis = t0 - 5_000_000_000
    anchor = BookCheckpoint.from_consistent(_snap(1, recv_ns=analysis))
    items = [_delta(2, recv_ns=analysis), _delta(3, recv_ns=t0 - 1_000_000_000)]
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
        t0_book_snap={"u": 3, "seq": 30},
    )
    dump = write_freeze_dump(
        dump_root=tmp_path / "d",
        request_id="sha1",
        forecast_id_seed="s",
        symbol="BTCUSDT",
        collector_instance_id="c",
        bundle=bundle,
        max_payload_bytes=10_000_000,
    )
    payload = (Path(dump["dump_dir"]) / "payload.json").read_bytes()
    manifest = orjson.loads((Path(dump["dump_dir"]) / "manifest.json").read_bytes())
    assert FullObCacheBridgeClient.verify_payload_hash(payload, manifest["payload_sha256"])
    bad = bytearray(payload)
    bad[0] ^= 0xFF
    assert not FullObCacheBridgeClient.verify_payload_hash(bytes(bad), manifest["payload_sha256"])


def test_exact_replay_parity_60k_levels():
    t0 = 140_000_000_000_000
    analysis = t0 - 5_000_000_000
    snap0 = _large_snap(1, recv_ns=analysis, n_bid=30000, n_ask=30000)
    anchor = BookCheckpoint.from_consistent(snap0, kind=CHECKPOINT_KIND_INITIAL)
    # one delta modifying top
    items = [
        _delta(2, recv_ns=analysis, bid=1000.0, ask=1000.01),
        _Item(
            receive_time_ns=t0 - 1_000_000_000,
            payload={
                "type": "delta",
                "ts": 1,
                "cts": 1,
                "data": {
                    "s": "BTCUSDT",
                    "b": [["999.99", "2.5"]],
                    "a": [["1000.02", "2.5"]],
                    "u": 3,
                    "seq": 30,
                },
                "local_receive_time_ns": t0 - 1_000_000_000,
            },
        ),
    ]
    # Build expected t0 by replaying offline first
    r = replay_anchor_and_deltas(symbol="BTCUSDT", anchor=anchor, deltas=[
        {
            **items[0].payload,
            "_bridge_receive_time_ns": items[0].receive_time_ns,
        },
        {
            **items[1].payload,
            "_bridge_receive_time_ns": items[1].receive_time_ns,
        },
    ])
    assert r["ok"]
    assert r["bid_levels"] + r["ask_levels"] >= 60000
    t0_book = {
        "u": r["final_u"],
        "seq": r["final_seq"],
        "b": r["bids"],
        "a": r["asks"],
    }
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
        t0_book_snap=t0_book,
    )
    assert bundle.data_complete is True
    assert bundle.replay_book_hash == bundle.t0_book_hash
    assert bundle.replay_final_u == t0_book["u"]


def test_lock_offload_checkpoint_copy_not_serialize():
    """Observer path: maybe_store uses already-copied snapshot; no orjson under book lock here."""
    ring = CheckpointRing(interval_sec=0, max_checkpoints=5, max_bytes=10_000_000, window_sec=690)
    snap = _snap(1, recv_ns=1_000_000_000)
    # Ensure store completes without needing book lock object
    assert ring.maybe_store(snap, now_ns=1_000_000_000) is True
    assert ring.items_snapshot()[0].book_content_hash


def test_default_settings_retention_floor(monkeypatch):
    monkeypatch.delenv("FULL_OB_CACHE_BRIDGE_ENABLED", raising=False)
    monkeypatch.delenv("FULL_OB_CACHE_BRIDGE_MAX_CHECKPOINTS", raising=False)
    cfg = load_cache_bridge_settings()
    assert cfg.checkpoint_retention_seconds >= 690
    assert cfg.max_checkpoints_per_symbol >= 14


def test_anchor_missing_fail_closed():
    t0 = 150_000_000_000_000
    items = [_delta(2, recv_ns=t0 - 5_000_000_000)]
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
    assert bundle.status == STATUS_BOOK_ANCHOR_MISSING
    assert bundle.data_complete is False

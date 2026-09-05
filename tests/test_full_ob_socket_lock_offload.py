"""Lock-offload: depth=0 socket vs Full-OB writer must not share long `_book_lock` holds."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orderbook_analyse.orderbook_v2_live.full_book_state import FullBookState, aggregate_full_book
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.async_sink import (
    NonBlockingDeltaSink,
)
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.config import FlightRecorderSettings
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.event_writer import (
    ActiveEventWriter,
    new_event_id,
)
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.manager import FullObEdgeFlightRecorder
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.replay import replay_event_directory
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.watcher import EdgeLevel
from orderbook_analyse.orderbook_v2_live.full_ob_sync import DeltaOutcome
from orderbook_analyse.orderbook_v2_live.on_demand_full import FullBookOnDemandManager


def _dense_book(n_per_side: int = 30_000) -> FullBookState:
    book = FullBookState(symbol="BTCUSDT")
    mid = 100_000.0
    bids = [[str(mid - i * 0.1), "1"] for i in range(1, n_per_side + 1)]
    asks = [[str(mid + i * 0.1), "1"] for i in range(1, n_per_side + 1)]
    book.apply_snapshot(bids=bids, asks=asks, u=10_000, seq=50_000, ts_ms=1, cts_ms=1, mark_ready=True)
    if n_per_side >= 30_000:
        assert len(book.bids) + len(book.asks) >= 60_000
    return book


def _mgr() -> FullBookOnDemandManager:
    mgr = FullBookOnDemandManager(
        send_chunk=lambda *a, **k: None,
        confirmed_topics=[],
        settings={
            "enabled": True,
            "max_active_topics": 4,
            "heartbeat_sec": 15,
            "lease_ttl_sec": 45,
            "pilot_symbols": {"BTCUSDT", "DOGEUSDT"},
            "clip_pct": 50.0,
            "max_ui_bars": 600,
            "rest_url": "http://127.0.0.1/invalid",
        },
    )
    mgr._acquire(symbol="BTCUSDT", lease_id="chart-1")
    rt = mgr._get_runtime("BTCUSDT")
    rt.book = _dense_book()
    rt.subscription_state = "live"
    return mgr


def test_copy_under_lock_faster_than_aggregate_under_lock():
    book = _dense_book()
    lock = threading.Lock()
    t0 = time.perf_counter_ns()
    with lock:
        snap = book.copy_consistent_snapshot()
    copy_ns = time.perf_counter_ns() - t0
    t1 = time.perf_counter_ns()
    with lock:
        aggregate_full_book(book)
    agg_ns = time.perf_counter_ns() - t1
    assert snap.update_id == 10_000 and snap.seq == 50_000
    assert copy_ns < agg_ns
    assert copy_ns < 200_000_000, copy_ns


def test_lock_copy_is_fast_observer_io_outside_lock():
    mgr = _mgr()

    def slow_observer(**kwargs):
        assert mgr._book_lock.acquire(blocking=False)
        mgr._book_lock.release()
        time.sleep(0.05)

    mgr.add_observer(slow_observer)
    payload = {
        "topic": "orderbook.full.BTCUSDT",
        "type": "delta",
        "ts": 2,
        "cts": 2,
        "data": {"s": "BTCUSDT", "b": [["99900.0", "2"]], "a": [], "u": 10_001, "seq": 50_010},
    }
    mgr.handle_message(payload, datetime.now(timezone.utc))
    assert mgr.lock_hold_ns_last < 30_000_000, mgr.lock_hold_ns_last
    snap = mgr._snapshot_response({"request_id": "t", "full_levels": True}, "BTCUSDT")
    assert snap["book_ready"] is True
    assert snap["update_id"] == 10_001
    assert snap["seq"] == 50_010
    assert snap["levels_capped_at_1000"] is False
    assert len(snap["full_bids"]) + len(snap["full_asks"]) >= 60_000
    assert snap["raw_bid_count"] + snap["raw_ask_count"] >= 60_000


def test_socket_snapshot_deadline_with_parallel_writer(tmp_path: Path):
    mgr = _mgr()
    writer = ActiveEventWriter(
        event_id="e1",
        symbol="BTCUSDT",
        directory=tmp_path / "e1",
        started_at=datetime.now(timezone.utc),
    )
    writer.open()
    sink = NonBlockingDeltaSink(writer, queue_size=4096)
    seen_lock_free = []

    def observe(**kwargs):
        seen_lock_free.append(mgr._book_lock.acquire(blocking=False))
        if seen_lock_free[-1]:
            mgr._book_lock.release()
        sink.try_put(dict(kwargs["payload"]))

    mgr.add_observer(observe)
    stop = threading.Event()

    def pump():
        u = 10_001
        seq = 50_010
        while not stop.is_set():
            mgr.handle_message(
                {
                    "topic": "orderbook.full.BTCUSDT",
                    "type": "delta",
                    "ts": u,
                    "cts": u,
                    "data": {
                        "s": "BTCUSDT",
                        "b": [[str(99900.0 + (u % 7) * 0.1), "1"]],
                        "a": [],
                        "u": u,
                        "seq": seq,
                    },
                },
                datetime.now(timezone.utc),
            )
            u += 1
            seq += 10
            time.sleep(0.002)

    th = threading.Thread(target=pump, daemon=True)
    th.start()
    time.sleep(0.05)
    t0 = time.perf_counter()
    snap = mgr._snapshot_response({"request_id": "live", "full_levels": False}, "BTCUSDT")
    dt = time.perf_counter() - t0
    stop.set()
    th.join(timeout=2)
    sink.stop()
    assert dt < 0.5, dt
    assert snap["ok"] is True
    assert snap["update_id"] is not None and snap["seq"] is not None
    assert snap.get("raw_bid_count", 0) > 1000
    assert any(seen_lock_free)
    writer.finalize(ended_at=datetime.now(timezone.utc), status="COMPLETE_REPLAYABLE", report_md="# t\n")


def test_queue_full_is_visible_not_silent():
    gate = threading.Event()

    class SlowWriter:
        symbol = "BTCUSDT"
        continuation_index = 0
        queue_drops = 0

        def append_delta(self, record):
            gate.wait(timeout=5)

        def append_delta_batch(self, records):
            gate.wait(timeout=5)
            return 0, 0

        def flush_pending(self):
            return None

        def mark_incomplete(self, reason):
            self.queue_drops += 1

    sink = NonBlockingDeltaSink(SlowWriter(), queue_size=1)
    try:
        assert sink.try_put({"u": 1}) is True
        deadline = time.time() + 1.0
        while sink.backlog > 0 and time.time() < deadline:
            time.sleep(0.01)
        assert sink.try_put({"u": 2}) is True
        assert sink.try_put({"u": 3}) is False
        assert sink.drops >= 1
    finally:
        gate.set()
        sink.stop()


def test_consistent_snapshot_u_seq_same_version():
    book = _dense_book(1000)
    book.apply_delta(bids=[["99999.9", "3"]], asks=[], u=10_001, seq=50_001, ts_ms=2, cts_ms=2)
    snap = book.copy_consistent_snapshot()
    assert snap.update_id == 10_001
    assert snap.seq == 50_001
    assert snap.bids[99999.9] == 3.0
    assert book.apply_delta(bids=[], asks=[], u=10_003, seq=50_003, ts_ms=3, cts_ms=3) is DeltaOutcome.GAP
    assert snap.update_id == 10_001
    bb, ba = snap.best_bid(), snap.best_ask()
    assert bb is not None and ba is not None and bb < ba


def test_segment_continuation_replay_and_no_duplicate_event(tmp_path: Path):
    settings = FlightRecorderSettings(
        enabled=True,
        symbols=frozenset({"BTCUSDT"}),
        capture_root=tmp_path,
        segment_minutes=30.0,
        queue_size=64,
        min_free_disk_gb=0.0,
        warn_free_disk_gb=0.0,
    )
    snap = {
        "s": "BTCUSDT",
        "b": [["100", "2"], ["99", "1"]],
        "a": [["101", "2"], ["102", "1"]],
        "u": 10,
        "seq": 100,
        "ts": 1000,
        "cts": 999,
    }

    class FakeFull:
        def __init__(self):
            self.last_rest_snapshot = snap
            self.runtimes = {
                "BTCUSDT": type(
                    "RT",
                    (),
                    {"last_rest_snapshot": snap, "gap_count": 0, "reconnect_count": 0},
                )()
            }

        def add_observer(self, cb):
            return None

        def _acquire(self, **kwargs):
            return None, True

        def _release(self, lease_id):
            return None, True

    fake = FakeFull()
    fr = FullObEdgeFlightRecorder(settings, full_book_manager=fake)
    fr.attach(fake)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=5)
    edges = (EdgeLevel("TPO_VAH", 100.0, "p1", cutoff),)
    fr.watcher.set_edges("BTCUSDT", edges, {"profile_id": "p1", "cutoff": cutoff.isoformat()})
    d1 = type(
        "D",
        (),
        {
            "reason": "test",
            "edge": edges[0],
            "sample": None,
            "trigger_source": "CROSS_IN",
            "edge_entry_crossed": True,
            "bootstrap_status": "N/A",
            "prior_zone_state": "OUT",
            "trigger_zone_state": "IN",
        },
    )()
    fr._start_or_merge_event("BTCUSDT", d1, now, book_ready=True)
    eid = fr._writers["BTCUSDT"].event_id
    fight = fr._writers["BTCUSDT"].fight_event_id
    fr._start_or_merge_event("BTCUSDT", d1, now + timedelta(seconds=1), book_ready=True)
    assert list(fr._writers.keys()) == ["BTCUSDT"]
    assert fr._writers["BTCUSDT"].event_id == eid

    sink = fr._sinks["BTCUSDT"]
    assert sink.try_put(
        {
            "type": "delta",
            "ts": 1011,
            "cts": 1010,
            "data": {"u": 11, "seq": 111, "b": [["100", "1"]], "a": []},
        }
    )
    time.sleep(0.15)
    fr._writers["BTCUSDT"].started_at = now - timedelta(minutes=31)
    fr._maybe_rotate_segment("BTCUSDT", datetime.now(timezone.utc))
    w2 = fr._writers["BTCUSDT"]
    assert w2.continuation_index == 1
    assert w2.fight_event_id == fight
    assert w2.event_id != eid
    root = w2.directory.parent
    man = json.loads((root / "manifest.json").read_text())
    assert man["completion_status"] == "SEGMENT_CONTINUED"
    assert man["fight_event_id"] == fight
    # Night-drop fix: queue + writer thread survive segment rollover.
    assert sink is fr._sinks["BTCUSDT"]
    assert sink.writer is fr._writers["BTCUSDT"]
    assert sink.writer.continuation_index == 1
    assert fr._sinks["BTCUSDT"].try_put(
        {"type": "delta", "ts": 1012, "cts": 1011, "data": {"u": 12, "seq": 112, "b": [["100", "1"]], "a": []}}
    )
    time.sleep(0.15)
    health = fr.health_dict()
    assert "writer_backlog" in health
    assert "open_tmp_bytes" in health
    assert "disk_free_gb" in health
    assert "projected_daily_bytes" in health
    fr._finalize_event("BTCUSDT", datetime.now(timezone.utc), "COMPLETE_REPLAYABLE")
    replay = replay_event_directory(root)
    assert replay["ok"] is True
    assert replay["crossed"] is False
    assert replay["applied_deltas"] >= 2
    replay2 = replay_event_directory(root)
    assert replay == replay2


def test_replay_sha_fail_closed(tmp_path: Path):
    event_id = new_event_id("BTCUSDT", datetime.now(timezone.utc))
    directory = tmp_path / "BTCUSDT" / "2026-08-30" / event_id
    writer = ActiveEventWriter(
        event_id=event_id,
        symbol="BTCUSDT",
        directory=directory,
        started_at=datetime.now(timezone.utc),
        trigger_reason="test",
    )
    writer.open()
    writer.write_rest_snapshot(
        {
            "s": "BTCUSDT",
            "b": [["100", "2"]],
            "a": [["101", "2"]],
            "u": 10,
            "seq": 100,
            "ts": 1000,
            "cts": 999,
        }
    )
    writer.append_delta({"type": "delta", "ts": 1011, "cts": 1010, "data": {"u": 11, "seq": 111, "b": [], "a": []}})
    writer.finalize(ended_at=datetime.now(timezone.utc), status="COMPLETE_REPLAYABLE", report_md="# t\n")
    good = replay_event_directory(directory)
    assert good["ok"] is True
    delta_path = directory / "full_ob_raw_deltas.jsonl.zst"
    delta_path.write_bytes(delta_path.read_bytes() + b"x")
    bad = replay_event_directory(directory)
    assert bad["ok"] is False
    assert bad["error"] == "sha256_mismatch_deltas"

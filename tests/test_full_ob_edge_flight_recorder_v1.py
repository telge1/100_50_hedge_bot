"""Tests for Full-OB edge flight recorder (ringbuffer, watcher, lifecycle, replay)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import orjson
import pytest

from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.config import FlightRecorderSettings
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.event_writer import (
    ActiveEventWriter,
    new_event_id,
)
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.manager import FullObEdgeFlightRecorder
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.profiles import (
    ProfileBundle,
    StaticProfileProvider,
    last_completed_window,
)
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.replay import replay_event_directory
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.ringbuffer import BoundedRawRingBuffer
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.watcher import (
    EdgeLevel,
    EdgeWatcher,
    SymbolLifecycle,
)


def test_last_completed_window_causal():
    now = datetime(2026, 8, 30, 16, 17, tzinfo=timezone.utc)
    start, end = last_completed_window(now, window_minutes=30)
    assert start == datetime(2026, 8, 30, 15, 30, tzinfo=timezone.utc)
    assert end == datetime(2026, 8, 30, 16, 0, tzinfo=timezone.utc)
    assert end <= now


def test_ringbuffer_time_bound_and_overflow():
    buf = BoundedRawRingBuffer(window_sec=1.0, max_messages=5, max_bytes=10_000_000)
    base = 1_000_000_000_000
    for i in range(10):
        buf.append({"u": i, "type": "delta"}, receive_time_ns=base + i * 100_000_000)
    assert len(buf) <= 5
    # old messages outside window
    buf.append({"u": 99, "type": "delta"}, receive_time_ns=base + 5_000_000_000)
    assert all(it.receive_time_ns >= base + 4_000_000_000 for it in buf.snapshot())


def test_ringbuffer_flush_no_double_count():
    buf = BoundedRawRingBuffer(window_sec=60, max_messages=100, max_bytes=10_000_000)
    for i in range(3):
        buf.append({"u": i}, receive_time_ns=i)
    flushed = buf.flush()
    assert len(flushed) == 3
    assert len(buf) == 0
    assert buf.flush() == []


def test_watcher_hysteresis_and_isolation():
    w = EdgeWatcher(
        arm_bps=50,
        capture_bps=20,
        disarm_bps=75,
        fast_approach_bps_per_sec=8,
        cooldown_minutes=5,
        acceptance_hold_sec=60,
    )
    cutoff = datetime(2026, 8, 30, 16, 0, tzinfo=timezone.utc)
    edges = (EdgeLevel("TPO_VAH", 100.0, "p1", cutoff), EdgeLevel("TPO_VAL", 90.0, "p1", cutoff))
    w.set_edges("BTCUSDT", edges, {"profile_id": "p1"})
    w.set_edges("DOGEUSDT", edges, {"profile_id": "p2"})

    now = cutoff + timedelta(minutes=5)
    # far
    d = w.evaluate("BTCUSDT", mid=110.0, now=now)
    assert d.action == "none"
    # approach from below VAH (no cross)
    d = w.evaluate("BTCUSDT", mid=99.6, now=now)
    assert d.action == "arm"
    assert w.state("BTCUSDT").lifecycle is SymbolLifecycle.ARMED
    # DOGE still idle
    assert w.state("DOGEUSDT").lifecycle is SymbolLifecycle.IDLE
    # hysteresis: still armed at ~60 bps
    d = w.evaluate("BTCUSDT", mid=99.4, now=now + timedelta(seconds=1))
    assert d.action == "none"
    assert w.state("BTCUSDT").lifecycle is SymbolLifecycle.ARMED
    # disarm > 75 bps
    d = w.evaluate("BTCUSDT", mid=99.2, now=now + timedelta(seconds=2))
    assert d.action == "disarm"


def test_watcher_fast_approach_does_not_trigger_without_zone_entry():
    w = EdgeWatcher(
        arm_bps=50,
        capture_bps=20,
        disarm_bps=75,
        fast_approach_bps_per_sec=5,
        cooldown_minutes=5,
        acceptance_hold_sec=60,
    )
    cutoff = datetime(2026, 8, 30, 16, 0, tzinfo=timezone.utc)
    edges = (EdgeLevel("VOL_VAH", 100.0, "p1", cutoff),)
    w.set_edges("BTCUSDT", edges, {})
    t0 = cutoff + timedelta(minutes=1)
    w.evaluate("BTCUSDT", mid=99.6, now=t0)  # arm (~40 bps)
    d = w.evaluate("BTCUSDT", mid=99.75, now=t0 + timedelta(seconds=1))  # 25 bps, still outside 20
    assert d.action != "trigger"
    d = w.evaluate("BTCUSDT", mid=99.85, now=t0 + timedelta(seconds=2))  # 15 bps → CROSS_IN
    assert d.action == "trigger"
    assert d.trigger_source == "CROSS_IN"


def test_no_duplicate_event_on_overlapping_triggers(tmp_path: Path):
    settings = FlightRecorderSettings(
        enabled=True,
        symbols=frozenset({"BTCUSDT"}),
        capture_root=tmp_path,
        arm_distance_bps=50,
        capture_distance_bps=20,
        disarm_distance_bps=75,
        ringbuffer_minutes=5,
        minimum_post_capture_minutes=0.01,
        reclaim_post_capture_minutes=0.01,
        maximum_event_minutes=90,
        cooldown_minutes=0.01,
        profile_poll_sec=999,
    )
    cutoff = datetime(2026, 8, 30, 16, 0, tzinfo=timezone.utc)
    edges = (
        EdgeLevel("TPO_VAH", 100.0, "p1", cutoff),
        EdgeLevel("VOL_VAH", 100.1, "p1", cutoff),
    )
    provider = StaticProfileProvider(
        {
            "BTCUSDT": ProfileBundle(
                symbol="BTCUSDT",
                profile_id="p1",
                session_start=cutoff - timedelta(minutes=30),
                cutoff=cutoff,
                edges=edges,
                meta={},
            )
        }
    )

    class FakeBook:
        book_ready = True
        update_id = 10
        seq = 100
        cts_ms = 1
        gap_count = 0
        reconnect_count = 0
        last_rest_snapshot = {
            "s": "BTCUSDT",
            "b": [["99", "1"]],
            "a": [["101", "1"]],
            "u": 10,
            "seq": 100,
            "ts": 1,
            "cts": 1,
        }

        def mid(self):
            return 100.05

    class FakeRuntime:
        book = FakeBook()
        gap_count = 0
        reconnect_count = 0
        last_rest_snapshot = FakeBook.last_rest_snapshot

    class FakeFull:
        runtimes = {"BTCUSDT": FakeRuntime()}

        def add_observer(self, cb):
            self.cb = cb

        def _acquire(self, **kwargs):
            return None, True

        def _release(self, lease_id):
            return None, True

    fr = FullObEdgeFlightRecorder(settings, profile_provider=provider, full_book_manager=FakeFull())
    fr.attach(FakeFull())
    # seed buffer
    for i in range(3):
        fr.on_full_ob_message(
            symbol="BTCUSDT",
            payload={"type": "delta", "data": {"u": 11 + i, "seq": 101 + i, "b": [], "a": []}, "ts": i},
            received_at=datetime.now(timezone.utc),
            receive_time_ns=i,
            phase="live",
            outcome="applied",
        )
    now = cutoff + timedelta(minutes=2)
    fr.watcher.set_edges("BTCUSDT", edges, {"profile_id": "p1", "cutoff": cutoff.isoformat()})
    # Exit zone first so bootstrap cannot open a file; then CROSS_IN.
    fr.watcher.evaluate("BTCUSDT", 99.0, now=now)  # outside / approach
    fr.watcher.evaluate("BTCUSDT", 99.6, now=now + timedelta(milliseconds=10))  # arm
    d1 = fr.watcher.evaluate("BTCUSDT", 100.05, now=now + timedelta(seconds=1))
    assert d1.action == "trigger"
    assert d1.trigger_source == "CROSS_IN"
    fr._start_or_merge_event("BTCUSDT", d1, now + timedelta(seconds=1), book_ready=True)
    eid = fr._writers["BTCUSDT"].event_id
    d2 = fr.watcher.evaluate("BTCUSDT", 100.05, now=now + timedelta(seconds=2))
    # still capturing → extend, merge
    fr.watcher.state("BTCUSDT").lifecycle = SymbolLifecycle.CAPTURING
    fr._start_or_merge_event("BTCUSDT", d1, now + timedelta(seconds=2), book_ready=True)
    assert list(fr._writers.keys()) == ["BTCUSDT"]
    assert fr._writers["BTCUSDT"].event_id == eid


def test_replay_deterministic(tmp_path: Path):
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
    snap = {
        "s": "BTCUSDT",
        "b": [["100", "2"], ["99", "1"]],
        "a": [["101", "2"], ["102", "1"]],
        "u": 10,
        "seq": 100,
        "ts": 1000,
        "cts": 999,
    }
    writer.write_rest_snapshot(snap)
    for u in (11, 12, 13):
        writer.append_delta(
            {
                "type": "delta",
                "ts": 1000 + u,
                "cts": 999 + u,
                "data": {"u": u, "seq": 100 + u, "b": [["100", "1"]], "a": []},
                "local_receive_time_ns": u,
            }
        )
    writer.finalize(ended_at=datetime.now(timezone.utc), status="COMPLETE_REPLAYABLE", report_md="# t\n")
    r1 = replay_event_directory(directory)
    r2 = replay_event_directory(directory)
    assert r1["ok"] is True
    assert r1 == r2
    assert r1["final_u"] == 13

    # tamper deltas → fail closed
    delta_path = directory / "full_ob_raw_deltas.jsonl.zst"
    delta_path.write_bytes(delta_path.read_bytes() + b"x")
    bad = replay_event_directory(directory)
    assert bad["ok"] is False


def test_settings_disabled_by_default():
    from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.config import (
        load_flight_recorder_settings,
    )
    import os

    os.environ.pop("OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE", None)
    cfg = load_flight_recorder_settings()
    assert cfg.enabled is False

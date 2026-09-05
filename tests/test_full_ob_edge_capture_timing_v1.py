"""Deterministic capture-timing contract full_ob_edge_capture_timing_v1."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.async_sink import NonBlockingDeltaSink
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.capture_plan import TIMING_CONTRACT
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.config import FlightRecorderSettings
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.manager import FullObEdgeFlightRecorder
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.replay import replay_event_directory
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.watcher import (
    EdgeLevel,
    EdgeWatcher,
    SymbolLifecycle,
)


def _settings(tmp_path: Path, **over) -> FlightRecorderSettings:
    kw = dict(
        enabled=True,
        symbols=frozenset({"BTCUSDT"}),
        capture_root=tmp_path,
        ringbuffer_minutes=10.0,
        minimum_post_capture_minutes=60.0,
        reclaim_post_capture_minutes=10.0,
        maximum_event_minutes=180.0,
        extension_minutes=30.0,
        segment_minutes=30.0,
        cooldown_minutes=5.0,
        profile_poll_sec=9999,
        queue_size=4096,
        min_free_disk_gb=0.0,
        warn_free_disk_gb=0.0,
    )
    kw.update(over)
    return FlightRecorderSettings(**kw)


def _fake():
    snap = {
        "s": "BTCUSDT",
        "b": [["100", "2"], ["99", "1"]],
        "a": [["101", "2"], ["102", "1"]],
        "u": 50,
        "seq": 500,
        "ts": 1_000,
        "cts": 999,
    }

    class Book:
        book_ready = True
        update_id = 50
        seq = 500
        last_receive_time_ns = 1_000_000_000
        gap_count = 0
        reconnect_count = 0

        def mid(self):
            return 100.05

    class RT:
        book = Book()
        gap_count = 0
        reconnect_count = 0
        last_rest_snapshot = snap

    class Full:
        runtimes = {"BTCUSDT": RT()}

        def add_observer(self, cb):
            self.cb = cb

        def _acquire(self, **kwargs):
            return None, True

        def _release(self, lease_id):
            return None, True

    return Full()


def _edges(cutoff):
    return (
        EdgeLevel("TPO_VAH", 100.0, "p1", cutoff),
        EdgeLevel("TPO_VAL", 90.0, "p1", cutoff),
    )


def _start_cross(fr: FullObEdgeFlightRecorder, t0: datetime) -> datetime:
    cutoff = t0 - timedelta(minutes=5)
    fr.watcher.set_edges(
        "BTCUSDT",
        _edges(cutoff),
        {"profile_id": "p1", "cutoff": cutoff.isoformat(), "session_start": cutoff.isoformat()},
    )
    fr.watcher.evaluate("BTCUSDT", 99.6, now=t0)
    d = fr.watcher.evaluate("BTCUSDT", 99.85, now=t0 + timedelta(seconds=1))
    assert d.action == "trigger"
    assert d.trigger_source == "CROSS_IN"
    fr._start_or_merge_event("BTCUSDT", d, t0 + timedelta(seconds=1), book_ready=True)
    return t0 + timedelta(seconds=1)


def test_prebuffer_10min_taken(tmp_path: Path):
    fr = FullObEdgeFlightRecorder(_settings(tmp_path), full_book_manager=_fake())
    t0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    fr._started_at = t0 - timedelta(seconds=800)
    base_ns = int((t0 - timedelta(seconds=650)).timestamp() * 1e9)
    for i in range(20):
        fr.on_full_ob_message(
            symbol="BTCUSDT",
            payload={
                "type": "delta",
                "ts": 1_700_000_000_000 + i,
                "data": {"u": 10 + i, "seq": 100 + i, "b": [], "a": []},
            },
            received_at=t0,
            receive_time_ns=base_ns + i * 30_000_000_000,
            phase="live",
            outcome="applied",
        )
    trig = _start_cross(fr, t0)
    plan = fr._plans["BTCUSDT"]
    assert plan.first_persisted_ts is not None
    assert plan.first_persisted_ts <= trig
    assert plan.pre_trigger_seconds_actual > 0
    assert plan.pre_trigger_seconds_actual >= 600 - 1
    fr.shutdown()


def test_cannot_close_before_3600(tmp_path: Path):
    fr = FullObEdgeFlightRecorder(_settings(tmp_path), full_book_manager=_fake())
    t0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    trig = _start_cross(fr, t0)
    fr.watcher.evaluate("BTCUSDT", 99.0, now=trig + timedelta(minutes=2))
    fr._maybe_end_event("BTCUSDT", trig + timedelta(seconds=3599))
    assert "BTCUSDT" in fr._writers
    fr._maybe_end_event("BTCUSDT", trig + timedelta(seconds=3600))
    assert "BTCUSDT" not in fr._writers


def test_reclaim_at_2min_still_runs_60min(tmp_path: Path):
    fr = FullObEdgeFlightRecorder(_settings(tmp_path), full_book_manager=_fake())
    t0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    trig = _start_cross(fr, t0)
    fr.watcher.evaluate("BTCUSDT", 100.3, now=trig + timedelta(minutes=1))
    fr.watcher.evaluate("BTCUSDT", 99.9, now=trig + timedelta(minutes=2))
    fr._handle_open_event_tick(
        "BTCUSDT",
        fr.watcher.evaluate("BTCUSDT", 99.0, now=trig + timedelta(minutes=2)),
        trig + timedelta(minutes=2),
    )
    plan = fr._plans["BTCUSDT"]
    assert plan.result_ts is not None
    assert plan.result_kind == "RECLAIM"
    fr._maybe_end_event("BTCUSDT", trig + timedelta(minutes=2))
    assert "BTCUSDT" in fr._writers
    fr.watcher.evaluate("BTCUSDT", 99.0, now=trig + timedelta(seconds=3600))
    fr._maybe_end_event("BTCUSDT", trig + timedelta(seconds=3600))
    assert "BTCUSDT" not in fr._writers


def test_result_at_minute_58_runs_to_68(tmp_path: Path):
    fr = FullObEdgeFlightRecorder(_settings(tmp_path), full_book_manager=_fake())
    t0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    trig = _start_cross(fr, t0)
    t58 = trig + timedelta(minutes=58)
    fr.watcher.evaluate("BTCUSDT", 100.3, now=t58 - timedelta(seconds=1))
    fr.watcher.evaluate("BTCUSDT", 99.9, now=t58)
    fr._handle_open_event_tick("BTCUSDT", type("D", (), {"marker": None, "edge": None})(), t58)
    assert fr._plans["BTCUSDT"].result_ts is not None
    fr.watcher.evaluate("BTCUSDT", 99.0, now=trig + timedelta(minutes=60))
    fr._maybe_end_event("BTCUSDT", trig + timedelta(minutes=60))
    assert "BTCUSDT" in fr._writers
    fr._maybe_end_event("BTCUSDT", trig + timedelta(minutes=68))
    assert "BTCUSDT" not in fr._writers


def test_extension_at_60_and_90(tmp_path: Path):
    fr = FullObEdgeFlightRecorder(_settings(tmp_path), full_book_manager=_fake())
    t0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    trig = _start_cross(fr, t0)
    fr.watcher.evaluate("BTCUSDT", 99.85, now=trig + timedelta(seconds=3600))
    fr._maybe_end_event("BTCUSDT", trig + timedelta(seconds=3600))
    plan = fr._plans["BTCUSDT"]
    assert plan.extension_count == 1
    assert plan.extension_reason == "PRICE_STILL_IN_EDGE_ZONE"
    assert "BTCUSDT" in fr._writers
    fr.watcher.evaluate("BTCUSDT", 99.85, now=trig + timedelta(seconds=5400))
    fr._maybe_end_event("BTCUSDT", trig + timedelta(seconds=5400))
    assert plan.extension_count == 2
    fr.shutdown()


def test_hard_limit_unresolved(tmp_path: Path):
    fr = FullObEdgeFlightRecorder(_settings(tmp_path), full_book_manager=_fake())
    t0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    trig = _start_cross(fr, t0)
    fr.watcher.evaluate("BTCUSDT", 99.85, now=trig + timedelta(seconds=10800))
    fr._maybe_end_event("BTCUSDT", trig + timedelta(seconds=10800))
    assert "BTCUSDT" not in fr._writers
    man = json.loads(next(tmp_path.rglob("event_manifest.json")).read_text())
    assert man["outcome_status"] == "UNRESOLVED_AT_CAPTURE_LIMIT"
    assert man["finalization_reason"] == "MAX_CAPTURE_DURATION_REACHED"


def test_segment_does_not_end_event(tmp_path: Path):
    fr = FullObEdgeFlightRecorder(_settings(tmp_path), full_book_manager=_fake())
    t0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    trig = _start_cross(fr, t0)
    fr._writers["BTCUSDT"].started_at = trig - timedelta(minutes=31)
    fr._maybe_rotate_segment("BTCUSDT", trig)
    assert "BTCUSDT" in fr._writers
    w = fr._writers["BTCUSDT"]
    plan = fr._plans["BTCUSDT"]
    assert w.continuation_index == 1
    assert w.fight_event_id == plan.fight_event_id
    assert [s.continuation_index for s in plan.segments] == [0, 1]
    assert plan.segments[0].segment_sha256
    assert plan.segments[1].previous_segment_sha256 == plan.segments[0].segment_sha256
    fr2 = FullObEdgeFlightRecorder(_settings(tmp_path / "sz", max_open_tmp_bytes=200), full_book_manager=_fake())
    _start_cross(fr2, t0)
    fr2._sinks["BTCUSDT"].try_put(
        {"type": "delta", "ts": 2000, "data": {"u": 51, "seq": 501, "b": [["x" * 80, "1"]], "a": []}}
    )
    time.sleep(0.2)
    fr2._maybe_rotate_segment("BTCUSDT", t0 + timedelta(seconds=2))
    assert "BTCUSDT" in fr2._writers
    fr.shutdown()
    fr2.shutdown()


def test_retouch_no_second_event_and_rearm_new(tmp_path: Path):
    fr = FullObEdgeFlightRecorder(_settings(tmp_path, cooldown_minutes=0.0001), full_book_manager=_fake())
    t0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    trig = _start_cross(fr, t0)
    eid = fr._plans["BTCUSDT"].fight_event_id
    # Idle tick in-zone: no EDGE_RETOUCH spam (night-drop fix).
    d_idle = fr.watcher.evaluate("BTCUSDT", 99.85, now=trig + timedelta(seconds=2))
    assert d_idle.action == "extend"
    assert d_idle.marker is None
    # Outside then reclaim → single EDGE_RETOUCH
    fr.watcher.evaluate("BTCUSDT", 100.5, now=trig + timedelta(seconds=3))
    d = fr.watcher.evaluate("BTCUSDT", 99.85, now=trig + timedelta(seconds=4))
    assert d.action == "extend"
    assert d.marker == "EDGE_RETOUCH"
    fr._handle_open_event_tick("BTCUSDT", d, trig + timedelta(seconds=4))
    assert list(fr._writers) == ["BTCUSDT"]
    assert fr._plans["BTCUSDT"].retouch_count >= 1
    d2 = fr.watcher.evaluate("BTCUSDT", 90.05, now=trig + timedelta(seconds=6))
    fr._handle_open_event_tick("BTCUSDT", d2, trig + timedelta(seconds=6))
    if fr.settings.nested_profile_signals_enabled:
        assert fr._plans["BTCUSDT"].secondary_edge_observation_count >= 1
    else:
        assert fr._plans["BTCUSDT"].secondary_edge_count >= 1
    fr.watcher.evaluate("BTCUSDT", 99.0, now=trig + timedelta(seconds=3600))
    fr._maybe_end_event("BTCUSDT", trig + timedelta(seconds=3600))
    assert "BTCUSDT" not in fr._writers
    later = trig + timedelta(seconds=3600, milliseconds=20)
    fr.watcher.evaluate("BTCUSDT", 99.0, now=later)
    assert fr.watcher.state("BTCUSDT").lifecycle is SymbolLifecycle.REARMED
    d3 = fr.watcher.evaluate("BTCUSDT", 99.85, now=later + timedelta(seconds=1))
    assert d3.action == "trigger"
    assert d3.trigger_source == "CROSS_IN"
    fr._start_or_merge_event("BTCUSDT", d3, later + timedelta(seconds=1), book_ready=True)
    assert fr._plans["BTCUSDT"].fight_event_id != eid
    fr.shutdown()


def test_bootstrap_not_cross_in():
    w = EdgeWatcher(
        arm_bps=50,
        capture_bps=20,
        disarm_bps=75,
        fast_approach_bps_per_sec=8,
        cooldown_minutes=5,
        acceptance_hold_sec=60,
    )
    cutoff = datetime(2026, 9, 3, tzinfo=timezone.utc)
    w.set_edges("BTCUSDT", _edges(cutoff), {})
    d = w.evaluate("BTCUSDT", 99.9, now=cutoff)
    assert d.action == "bootstrap_observe"
    assert d.trigger_source == "BOOTSTRAP_ALREADY_IN_EDGE_ZONE"
    assert d.edge_entry_crossed is False
    # No second bootstrap spam while still inside.
    d2 = w.evaluate("BTCUSDT", 99.9, now=cutoff + timedelta(seconds=1))
    assert d2.action == "none"
    assert d2.reason == "bootstrap_waiting_exit_for_rearm"


def test_profile_update_does_not_change_trigger_edge(tmp_path: Path):
    fr = FullObEdgeFlightRecorder(_settings(tmp_path), full_book_manager=_fake())
    t0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    trig = _start_cross(fr, t0)
    frozen = fr._plans["BTCUSDT"].edge_price_at_trigger
    fr.watcher.set_edges("BTCUSDT", (EdgeLevel("TPO_VAH", 111.0, "p2", t0),), {"profile_id": "p2"})
    fr._handle_open_event_tick("BTCUSDT", type("D", (), {"marker": None, "edge": None})(), trig + timedelta(seconds=1))
    assert fr._plans["BTCUSDT"].edge_price_at_trigger == frozen
    assert any(m["marker_type"] == "PROFILE_UPDATE_DURING_CAPTURE" for m in fr._plans["BTCUSDT"].markers)
    fr.shutdown()


def test_ugap_and_queue_drop_incomplete(tmp_path: Path):
    fr = FullObEdgeFlightRecorder(_settings(tmp_path), full_book_manager=_fake())
    t0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    _start_cross(fr, t0)
    fr.on_full_ob_message(
        symbol="BTCUSDT",
        payload={"type": "delta", "data": {"u": 99, "seq": 1, "b": [], "a": []}},
        received_at=t0,
        receive_time_ns=2,
        phase="live",
        outcome="gap",
    )
    assert fr._plans["BTCUSDT"].data_quality == "INCOMPLETE"
    assert "U_GAP" in fr._plans["BTCUSDT"].incomplete_reasons
    fr.shutdown()
    gate = threading.Event()

    class Slow:
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
            pass

    sink = NonBlockingDeltaSink(Slow(), queue_size=1)
    assert sink.try_put({"u": 1})
    deadline = time.time() + 1
    while sink.backlog > 0 and time.time() < deadline:
        time.sleep(0.01)
    assert sink.try_put({"u": 2})
    assert sink.try_put({"u": 3}) is False
    gate.set()
    sink.stop()


def test_shutdown_marks_interrupted(tmp_path: Path):
    fr = FullObEdgeFlightRecorder(_settings(tmp_path), full_book_manager=_fake())
    t0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    _start_cross(fr, t0)
    fr.shutdown()
    assert fr._writers == {}
    man = json.loads(next(tmp_path.rglob("manifest.json")).read_text())
    assert man["completion_status"] == "INTERRUPTED_BY_CONTROLLED_RESTART"


def test_replay_multi_segment_same_fight_id(tmp_path: Path):
    fr = FullObEdgeFlightRecorder(_settings(tmp_path), full_book_manager=_fake())
    t0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    trig = _start_cross(fr, t0)
    fr._sinks["BTCUSDT"].try_put(
        {"type": "delta", "ts": 2000, "cts": 2000, "data": {"u": 51, "seq": 510, "b": [["100", "1"]], "a": []}}
    )
    time.sleep(0.15)
    fr._writers["BTCUSDT"].started_at = trig - timedelta(minutes=31)
    fr._maybe_rotate_segment("BTCUSDT", trig)
    fr._sinks["BTCUSDT"].try_put(
        {"type": "delta", "ts": 2001, "cts": 2001, "data": {"u": 52, "seq": 520, "b": [["100", "1"]], "a": []}}
    )
    time.sleep(0.15)
    fight = fr._plans["BTCUSDT"].fight_event_id
    root = fr._writers["BTCUSDT"].directory.parent
    fr.watcher.evaluate("BTCUSDT", 99.0, now=trig + timedelta(seconds=3600))
    fr._maybe_end_event("BTCUSDT", trig + timedelta(seconds=3600))
    replay = replay_event_directory(root)
    replay2 = replay_event_directory(root)
    assert replay == replay2
    mans = list(root.rglob("manifest.json"))
    ids = {json.loads(p.read_text())["fight_event_id"] for p in mans}
    assert ids == {fight}
    idxs = sorted(json.loads(p.read_text())["continuation_index"] for p in mans)
    assert idxs == list(range(len(idxs)))
    ev = json.loads((root / "event_manifest.json").read_text())
    assert ev["timing_contract"] == TIMING_CONTRACT
    assert ev["segment_count"] >= 2

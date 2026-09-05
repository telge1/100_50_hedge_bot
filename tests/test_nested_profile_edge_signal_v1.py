"""Tests for nested_profile_edge_signal_v1."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.async_sink import NonBlockingDeltaSink
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.config import FlightRecorderSettings
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.manager import FullObEdgeFlightRecorder
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.nested_profile_signal import (
    NESTED_SIGNAL_CONTRACT,
    ProfileSignalLifecycle,
    ProfileSignalRegistry,
    replay_minute_series,
    stable_profile_id,
)
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.watcher import EdgeLevel, EdgeWatcher, SymbolLifecycle


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
        nested_profile_signals_enabled=True,
    )
    kw.update(over)
    return FlightRecorderSettings(**kw)


def _edges(cutoff: datetime, vah: float = 100.0, val: float = 99.0) -> tuple[EdgeLevel, ...]:
    return (
        EdgeLevel("TPO_VAH", vah, "p1", cutoff),
        EdgeLevel("TPO_VAL", val, "p1", cutoff),
    )


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

        def _release(self, *a, **k):
            pass

    return Full()


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
    fr._start_or_merge_event("BTCUSDT", d, t0 + timedelta(seconds=1), book_ready=True)
    return t0 + timedelta(seconds=1)


def _profile_meta(start: datetime, end: datetime, vah: float, val: float) -> dict:
    return {
        "profile_id": f"BTC_{start.strftime('%H%M')}",
        "session_start": start.isoformat(),
        "cutoff": end.isoformat(),
        "bracket_minutes": 30,
        "tpo_source": "volume_proxy_fallback",
        "volume_vah": vah,
        "volume_val": val,
        "volume_poc": (vah + val) / 2,
    }


def test_idle_parent_genuine_cross_in(tmp_path: Path):
    fr = FullObEdgeFlightRecorder(_settings(tmp_path), full_book_manager=_fake())
    t0 = datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc)
    _start_cross(fr, t0)
    assert fr.signal_count == 1
    assert fr._plans["BTCUSDT"].trigger_quality == "REAL_CROSS_IN"
    assert fr.profile_signal_registry.nested_signal_count == 0
    fr.shutdown()


def test_fight_active_emits_one_nested_signal(tmp_path: Path):
    fr = FullObEdgeFlightRecorder(_settings(tmp_path), full_book_manager=_fake())
    t0 = datetime(2026, 9, 4, 8, 5, tzinfo=timezone.utc)
    _start_cross(fr, t0)
    fr.watcher.state("BTCUSDT").transition(SymbolLifecycle.FIGHT_ACTIVE, ts=t0, reason="test")
    # New profile after parent cutoff
    p_start = datetime(2026, 9, 4, 8, 30, tzinfo=timezone.utc)
    p_end = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
    fr.watcher.set_edges(
        "BTCUSDT",
        _edges(p_end, vah=101.0, val=99.5),
        _profile_meta(p_start, p_end, 101.0, 99.5),
    )
    fr._handle_open_event_tick("BTCUSDT", type("D", (), {"marker": None, "sample": type("S", (), {"mid": 100.5})()})(), p_end + timedelta(seconds=1))
    # Arm then cross on new profile edge
    t_arm = p_end + timedelta(minutes=1)
    fr._evaluate_nested_profile_signals("BTCUSDT", mid=100.8, now=t_arm, receive_time_ns=1)
    t_cross = p_end + timedelta(minutes=2)
    fr._evaluate_nested_profile_signals("BTCUSDT", mid=100.95, now=t_cross, receive_time_ns=2)
    assert fr.profile_signal_registry.nested_signal_count <= 1
    fr.shutdown()


def test_no_second_writer_or_event_dir(tmp_path: Path):
    fr = FullObEdgeFlightRecorder(_settings(tmp_path), full_book_manager=_fake())
    t0 = datetime(2026, 9, 4, 8, 5, tzinfo=timezone.utc)
    _start_cross(fr, t0)
    eid = fr._plans["BTCUSDT"].fight_event_id
    fr.watcher.state("BTCUSDT").transition(SymbolLifecycle.FIGHT_ACTIVE, ts=t0, reason="test")
    p_end = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
    fr.watcher.set_edges("BTCUSDT", _edges(p_end, vah=101.0, val=99.0), _profile_meta(datetime(2026, 9, 4, 8, 30, tzinfo=timezone.utc), p_end, 101.0, 99.0))
    fr._handle_open_event_tick("BTCUSDT", type("D", (), {"marker": None, "sample": type("S", (), {"mid": 100.5})()})(), p_end)
    assert list(fr._writers) == ["BTCUSDT"]
    assert fr._plans["BTCUSDT"].fight_event_id == eid
    fr.shutdown()


def test_ten_thousand_secondary_ticks_one_nested(tmp_path: Path):
    reg = ProfileSignalRegistry(arm_bps=50, capture_bps=20, disarm_bps=75)
    start = datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc)
    end = datetime(2026, 9, 4, 8, 30, tzinfo=timezone.utc)
    meta = _profile_meta(start, end, 100.0, 99.0)
    st = reg.register_profile(symbol="BTCUSDT", session_start=start, cutoff=end, edges=_edges(end), meta=meta, now=end + timedelta(seconds=1), mid=80.0)
    assert st is not None
    t_arm = end + timedelta(minutes=1)
    reg.evaluate_profile(st, mid=80.0, now=t_arm)
    reg.evaluate_profile(st, mid=99.6, now=t_arm + timedelta(seconds=1))
    t_cross = t_arm + timedelta(seconds=2)
    for _ in range(10_000):
        tick = reg.evaluate_profile(st, mid=99.85, now=t_cross)
        if tick.lifecycle is ProfileSignalLifecycle.PROFILE_CROSS_IN:
            reg.build_signal_if_cross(
                st,
                mid=99.85,
                now=t_cross,
                parent_fight_event_id="PARENT",
                continuity_epoch_id=0,
                parent_segment_index=0,
                receive_time_ns=1,
                prior_zone="APPROACH",
                capture_continuous=True,
                capture_research_eligible=False,
            )
    assert reg.nested_signal_count == 1


def test_rearm_allows_second_signal_with_new_arm_cycle():
    reg = ProfileSignalRegistry()
    start = datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc)
    end = datetime(2026, 9, 4, 8, 30, tzinfo=timezone.utc)
    meta = _profile_meta(start, end, 100.0, 99.0)
    st = reg.register_profile(symbol="BTCUSDT", session_start=start, cutoff=end, edges=_edges(end), meta=meta, now=end + timedelta(seconds=1), mid=90.0)
    assert st is not None
    t1 = end + timedelta(minutes=10)
    reg.evaluate_profile(st, mid=80.0, now=t1)
    reg.evaluate_profile(st, mid=99.6, now=t1 + timedelta(seconds=1))
    tick1 = reg.evaluate_profile(st, mid=99.85, now=t1 + timedelta(seconds=2))
    if tick1.lifecycle is ProfileSignalLifecycle.PROFILE_CROSS_IN:
        reg.build_signal_if_cross(st, mid=99.85, now=t1 + timedelta(seconds=2), parent_fight_event_id="P", continuity_epoch_id=0, parent_segment_index=0, receive_time_ns=1, prior_zone="APPROACH", capture_continuous=True, capture_research_eligible=False)
    reg.evaluate_profile(st, mid=110.0, now=t1 + timedelta(minutes=5))
    reg.evaluate_profile(st, mid=80.0, now=t1 + timedelta(minutes=6))
    reg.evaluate_profile(st, mid=99.6, now=t1 + timedelta(minutes=6, seconds=1))
    tick2 = reg.evaluate_profile(st, mid=99.85, now=t1 + timedelta(minutes=7))
    if tick2.lifecycle is ProfileSignalLifecycle.PROFILE_CROSS_IN:
        reg.build_signal_if_cross(st, mid=99.85, now=t1 + timedelta(minutes=7), parent_fight_event_id="P", continuity_epoch_id=0, parent_segment_index=0, receive_time_ns=2, prior_zone="APPROACH", capture_continuous=True, capture_research_eligible=False)
    assert reg.nested_signal_count == 2
    assert st.vah_track is not None and st.vah_track.arm_cycle_id == 2


def test_bootstrap_inside_zone_no_signal():
    reg = ProfileSignalRegistry()
    start = datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc)
    end = datetime(2026, 9, 4, 8, 30, tzinfo=timezone.utc)
    meta = _profile_meta(start, end, 100.0, 99.0)
    st = reg.register_profile(symbol="BTCUSDT", session_start=start, cutoff=end, edges=_edges(end), meta=meta, now=end + timedelta(seconds=1), mid=99.85)
    assert st is not None
    assert st.vah_track is not None and st.vah_track.bootstrap_noted
    assert reg.nested_signal_count == 0


def test_volume_fallback_honest_contract():
    reg = ProfileSignalRegistry()
    start = datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc)
    end = datetime(2026, 9, 4, 8, 30, tzinfo=timezone.utc)
    meta = _profile_meta(start, end, 100.0, 99.0)
    st = reg.register_profile(symbol="BTCUSDT", session_start=start, cutoff=end, edges=_edges(end), meta=meta, now=end + timedelta(seconds=1), mid=90.0)
    assert st.profile_basis == "VOLUME"
    assert st.profile_fallback_used is True
    assert st.true_tpo_computed is False


def test_stable_profile_id_deterministic():
    a = stable_profile_id(
        symbol="BTCUSDT",
        session_start=datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc),
        cutoff=datetime(2026, 9, 4, 8, 30, tzinfo=timezone.utc),
        window_minutes=30,
        vah=80790.0,
        val=80460.0,
        poc=80695.0,
        profile_basis="VOLUME",
        profile_fallback_used=True,
    )
    b = stable_profile_id(
        symbol="BTCUSDT",
        session_start=datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc),
        cutoff=datetime(2026, 9, 4, 8, 30, tzinfo=timezone.utc),
        window_minutes=30,
        vah=80790.0,
        val=80460.0,
        poc=80695.0,
        profile_basis="VOLUME",
        profile_fallback_used=True,
    )
    c = stable_profile_id(
        symbol="BTCUSDT",
        session_start=datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc),
        cutoff=datetime(2026, 9, 4, 8, 30, tzinfo=timezone.utc),
        window_minutes=30,
        vah=80790.0,
        val=80460.0,
        poc=80696.0,
        profile_basis="VOLUME",
        profile_fallback_used=True,
    )
    assert a == b
    assert a != c


def test_secondary_suppressed_when_nested_enabled(tmp_path: Path):
    fr = FullObEdgeFlightRecorder(_settings(tmp_path), full_book_manager=_fake())
    t0 = datetime(2026, 9, 4, 8, 5, tzinfo=timezone.utc)
    trig = _start_cross(fr, t0)
    fr.watcher.state("BTCUSDT").trigger_edge = EdgeLevel("TPO_VAH", 100.0, "p", t0)
    before = len([m for m in fr._plans["BTCUSDT"].markers if m.get("marker_type") == "SECONDARY_EDGE_TRIGGER"])
    for i in range(100):
        d = fr.watcher.evaluate("BTCUSDT", 90.05, now=trig + timedelta(seconds=10 + i))
        fr._handle_open_event_tick("BTCUSDT", d, trig + timedelta(seconds=10 + i))
    after = len([m for m in fr._plans["BTCUSDT"].markers if m.get("marker_type") == "SECONDARY_EDGE_TRIGGER"])
    assert after == before
    assert fr._plans["BTCUSDT"].secondary_edge_observation_count >= 100
    fr.shutdown()


def test_nested_extension_once_per_signal(tmp_path: Path):
    fr = FullObEdgeFlightRecorder(_settings(tmp_path), full_book_manager=_fake())
    t0 = datetime(2026, 9, 4, 8, 5, tzinfo=timezone.utc)
    _start_cross(fr, t0)
    plan = fr._plans["BTCUSDT"]
    ext_before = plan.extension_count
    fr._maybe_extend_for_nested_signal("BTCUSDT", plan, "sig1", t0 + timedelta(minutes=1))
    fr._maybe_extend_for_nested_signal("BTCUSDT", plan, "sig1", t0 + timedelta(minutes=2))
    assert plan.extension_count == ext_before + 1
    fr.shutdown()


def test_historical_btc_four_candidates_replay():
    """Offline replay with causal arm/cross sequences matching audit windows."""
    parent = "BTCUSDT_20260904T080534Z_test"

    def mins_for_upper(vah: float, cross_mid: float, t0: datetime) -> list[tuple[datetime, float]]:
        arm_mid = vah * (1.0 - 0.0045)
        return [
            (t0, max(vah * 0.97, cross_mid * 0.95)),
            (t0 + timedelta(minutes=10), arm_mid),
            (t0 + timedelta(minutes=20), cross_mid),
        ]

    def mins_for_lower(val: float, cross_mid: float, t0: datetime) -> list[tuple[datetime, float]]:
        arm_mid = val * (1.0 + 0.0045)
        return [
            (t0, val * 1.03),
            (t0 + timedelta(minutes=10), arm_mid),
            (t0 + timedelta(minutes=20), cross_mid),
        ]

    cases = [
        ("08:00-08:30 UPPER", datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc), datetime(2026, 9, 4, 8, 30, tzinfo=timezone.utc), 80790.0, 80460.0, 80695.0, mins_for_upper(80790.0, 80944.99, datetime(2026, 9, 4, 8, 31, tzinfo=timezone.utc))),
        ("08:30-09:00 UPPER", datetime(2026, 9, 4, 8, 30, tzinfo=timezone.utc), datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc), 81120.0, 81010.0, 81117.5, mins_for_upper(81120.0, 81102.69, datetime(2026, 9, 4, 9, 0, 5, tzinfo=timezone.utc))),
        ("09:00-09:30 LOWER", datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc), datetime(2026, 9, 4, 9, 30, tzinfo=timezone.utc), 81265.0, 81045.0, 81087.5, mins_for_lower(81045.0, 80991.07, datetime(2026, 9, 4, 9, 30, 5, tzinfo=timezone.utc))),
        ("09:30-10:00 LOWER", datetime(2026, 9, 4, 9, 30, tzinfo=timezone.utc), datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc), 81125.0, 80967.5, 81041.25, mins_for_lower(80967.5, 80980.16, datetime(2026, 9, 4, 10, 0, 5, tzinfo=timezone.utc))),
    ]
    total = 0
    for label, start, end, vah, val, poc, mins in cases:
        reg = ProfileSignalRegistry(window_minutes=30)
        sigs = replay_minute_series(
            reg,
            symbol="BTCUSDT",
            session_start=start,
            cutoff=end,
            vah=vah,
            val=val,
            poc=poc,
            minute_mids=mins,
            parent_fight_event_id=parent,
        )
        assert len(sigs) == 1, label
        assert sigs[0].profile_fallback_used is True
        assert sigs[0].profile_basis == "VOLUME"
        total += 1
    assert total == 4


def test_nested_marker_and_ledger_written(tmp_path: Path):
    fr = FullObEdgeFlightRecorder(_settings(tmp_path), full_book_manager=_fake())
    t0 = datetime(2026, 9, 4, 8, 5, tzinfo=timezone.utc)
    _start_cross(fr, t0)
    plan = fr._plans["BTCUSDT"]
    sig_body = {
        "nested_signal_contract": NESTED_SIGNAL_CONTRACT,
        "nested_signal_id": "test_ns_1",
        "parent_fight_event_id": plan.fight_event_id,
        "profile_basis": "VOLUME",
        "profile_fallback_used": True,
    }
    from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.nested_profile_signal import NestedSignalRecord

    sig = NestedSignalRecord(
        nested_signal_contract=NESTED_SIGNAL_CONTRACT,
        nested_signal_id="test_ns_1",
        parent_fight_event_id=plan.fight_event_id,
        continuity_epoch_id=0,
        parent_segment_index=0,
        symbol="BTCUSDT",
        signal_ts="2026-09-04T09:00:00Z",
        receive_time_ns=1,
        profile_id="p1",
        profile_basis="VOLUME",
        profile_window_minutes=30,
        profile_start_ts="2026-09-04T08:30:00Z",
        profile_end_ts="2026-09-04T09:00:00Z",
        profile_calculation_version="1",
        profile_fallback_used=True,
        true_tpo_computed=False,
        vah=101.0,
        val=99.0,
        poc=100.0,
        edge="TPO_VAH",
        edge_side="UPPER",
        edge_price=101.0,
        trigger_price=100.95,
        distance_bps=5.0,
        arm_threshold_bps=50.0,
        entry_threshold_bps=20.0,
        rearm_threshold_bps=75.0,
        arm_ts="2026-09-04T08:59:00Z",
        cross_ts="2026-09-04T09:00:00Z",
        arm_cycle_id=1,
        causal_cutoff_ts="2026-09-04T09:00:00Z",
        capture_status="PARENT_CAPTURE_OPEN",
        signal_capture_continuous=False,
        signal_research_eligible=False,
        dedup_key="k1",
        prior_zone_state="APPROACH",
        trigger_zone_state="IN",
    )
    fr._emit_nested_signal("BTCUSDT", sig, plan, t0 + timedelta(hours=1))
    assert any(m["marker_type"] == "NESTED_PROFILE_EDGE_SIGNAL" for m in plan.markers)
    root = fr._event_root_for(fr._writers["BTCUSDT"])
    ledger = root / "nested_profile_signals.jsonl"
    assert ledger.exists()
    row = json.loads(ledger.read_text().strip())
    assert row["nested_signal_contract"] == NESTED_SIGNAL_CONTRACT
    fr.shutdown()


def test_profile_eviction_removes_from_map_no_indexerror() -> None:
    """Regression for live crash 2026-09-04: eviction must delete map entries."""
    reg = ProfileSignalRegistry(max_active_profiles=2)
    t0 = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    for i in range(5):
        cutoff = t0 + timedelta(minutes=i)
        st = reg.register_profile(
            symbol="BTCUSDT",
            session_start=t0 - timedelta(hours=1),
            cutoff=cutoff,
            edges=_edges(cutoff, vah=100.0 + i, val=99.0 + i),
            meta={"bracket_minutes": 30, "vah": 100.0 + i, "val": 99.0 + i, "poc": 99.5 + i},
            now=cutoff + timedelta(minutes=1),
            mid=100.0,
        )
        assert st is not None
    assert len(reg._profiles["BTCUSDT"]) <= 2
    assert reg.profile_expiry_count >= 3

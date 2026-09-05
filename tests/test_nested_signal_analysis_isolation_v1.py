"""Tests for nested_signal_analysis_isolation_v1."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.config import FlightRecorderSettings
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.manager import FullObEdgeFlightRecorder
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.nested_profile_signal import NestedSignalRecord, NESTED_SIGNAL_CONTRACT
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.signal_analysis_isolation import (
    ANALYSIS_ISOLATION_CONTRACT,
    REASON_INSUFFICIENT_SIGNAL_POST_COVERAGE,
    SignalMetricStore,
    TimeGap,
    assign_overlap_clusters,
    build_signal_analysis_contract,
    clickhouse_roundtrip_rows,
    evaluate_gap_matrix,
    idempotent_merge_import,
)
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.watcher import EdgeLevel, SymbolLifecycle


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
    return (EdgeLevel("TPO_VAH", vah, "p1", cutoff), EdgeLevel("TPO_VAL", val, "p1", cutoff))


def _fake():
    snap = {"s": "BTCUSDT", "b": [["100", "2"]], "a": [["101", "2"]], "u": 50, "seq": 500, "ts": 1_000, "cts": 999}

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
    fr.watcher.set_edges("BTCUSDT", _edges(cutoff), {"profile_id": "p1", "cutoff": cutoff.isoformat(), "session_start": cutoff.isoformat()})
    fr.watcher.evaluate("BTCUSDT", 99.6, now=t0)
    d = fr.watcher.evaluate("BTCUSDT", 99.85, now=t0 + timedelta(seconds=1))
    assert d.action == "trigger"
    fr._start_or_merge_event("BTCUSDT", d, t0 + timedelta(seconds=1), book_ready=True)
    return t0 + timedelta(seconds=1)


def _mk(signal_id: str, trigger: datetime, *, parent: str = "P1", profile: str = "profA", edge: str = "TPO_VAH", price: float = 100.0, epoch: int = 0):
    return build_signal_analysis_contract(
        signal_id=signal_id,
        parent_fight_event_id=parent,
        profile_id=profile,
        profile_basis="VOLUME",
        profile_start_ts=trigger - timedelta(minutes=30),
        profile_end_ts=trigger - timedelta(minutes=1),
        vah=price,
        val=price - 1,
        poc=price - 0.5,
        edge=edge,
        edge_price=price,
        trigger_ts=trigger,
        trigger_price=price - 0.1,
        continuity_epoch_id=epoch,
        capture_available_until=trigger + timedelta(hours=3),
    )


def test_two_signals_isolated_profiles_and_metrics():
    t0 = datetime(2026, 9, 4, 8, 5, tzinfo=timezone.utc)
    a = _mk("sigA", t0, profile="profA", edge="TPO_VAH", price=100.0)
    b = _mk("sigB", t0 + timedelta(hours=1), profile="profB", edge="TPO_VAL", price=99.0)
    store = SignalMetricStore()
    store.set_profile("sigA", {"vah": a.vah, "edge": a.edge})
    store.set_profile("sigB", {"vah": b.vah, "edge": b.edge})
    store.set_metric("sigA", "distance_bps", 5.0)
    store.set_metric("sigB", "distance_bps", 7.0)
    store.set_outcome("sigA", {"result": "ACCEPT"})
    store.set_outcome("sigB", {"result": "REJECT"})
    assert store.get_profile("sigA")["edge"] == "TPO_VAH"
    assert store.get_profile("sigB")["edge"] == "TPO_VAL"
    assert store.get_metric("sigA", "distance_bps") == 5.0
    assert store.get_metric("sigB", "distance_bps") == 7.0
    assert store.get_outcome("sigA")["result"] != store.get_outcome("sigB")["result"]
    assert store.try_cross_write("sigA", "sigB", "distance_bps", 999) is False
    assert store.contamination_attempts == 1
    assert store.get_metric("sigB", "distance_bps") == 7.0


def test_overlap_clustering_deterministic_and_order_independent():
    t0 = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
    # Windows: pre 10m + post 60m → ~70m; 20m apart ⇒ overlap
    a = _mk("sigA", t0)
    b = _mk("sigB", t0 + timedelta(minutes=20))
    c = _mk("sigC", t0 + timedelta(hours=5))  # far → isolated
    r1 = assign_overlap_clusters([a, b, c])
    r2 = assign_overlap_clusters([c, b, a])
    by1 = {x.signal_id: x for x in r1}
    by2 = {x.signal_id: x for x in r2}
    assert by1["sigA"].overlap_cluster_id == by1["sigB"].overlap_cluster_id
    assert by1["sigA"].overlap_cluster_id is not None
    assert by1["sigC"].overlap_cluster_id is None
    assert by1["sigA"].independent_observation is False
    assert by1["sigC"].independent_observation is True
    assert by1["sigA"].overlap_cluster_id == by2["sigA"].overlap_cluster_id
    assert set(by1["sigA"].overlapping_signal_ids) == {"sigB"}


def test_gap_affects_only_signal_a():
    t0 = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
    a = _mk("sigA", t0)
    b = _mk("sigB", t0 + timedelta(hours=3))
    gap = TimeGap(t0 + timedelta(minutes=5), t0 + timedelta(minutes=10))
    rows = evaluate_gap_matrix([a, b], [gap])
    by = {r["signal_id"]: r for r in rows}
    assert by["sigA"]["research_eligible"] is False
    assert by["sigB"]["research_eligible"] is True


def test_gap_affects_both_signals():
    t0 = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
    a = _mk("sigA", t0)
    b = _mk("sigB", t0 + timedelta(minutes=30))
    gap = TimeGap(t0 + timedelta(minutes=40), t0 + timedelta(minutes=50))
    rows = evaluate_gap_matrix([a, b], [gap])
    assert all(r["research_eligible"] is False for r in rows)


def test_gap_between_windows_both_eligible():
    t0 = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
    a = _mk("sigA", t0)
    # B starts far enough that windows don't contain the mid gap
    b = _mk("sigB", t0 + timedelta(hours=4))
    # Gap after A's post window ends (t0+60m) and before B's pre (t0+4h-10m)
    gap = TimeGap(t0 + timedelta(hours=2), t0 + timedelta(hours=2, minutes=5))
    rows = evaluate_gap_matrix([a, b], [gap])
    assert all(r["research_eligible"] is True for r in rows)


def test_local_epoch_replayable_parent_not_continuous():
    t0 = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)
    c = build_signal_analysis_contract(
        signal_id="ns1",
        parent_fight_event_id="P",
        profile_id="p",
        profile_basis="VOLUME",
        profile_start_ts=t0 - timedelta(minutes=30),
        profile_end_ts=t0 - timedelta(minutes=1),
        vah=100,
        val=99,
        poc=99.5,
        edge="TPO_VAH",
        edge_price=100,
        trigger_ts=t0,
        trigger_price=99.9,
        continuity_epoch_id=1,
        capture_available_until=t0 + timedelta(hours=2),
        signal_capture_continuous=True,
        parent_continuous_capture=False,  # parent had earlier resync
        parent_replayable=True,
        epoch_coverage_ok=True,
    )
    # Strict: continuous within signal window → eligible; parent global flag not auto-false
    assert c.continuous_capture is True
    assert c.replayable is True
    assert c.research_eligible is True


def test_insufficient_post_near_hard_cap():
    t0 = datetime(2026, 9, 4, 10, 30, tzinfo=timezone.utc)
    hard = t0 + timedelta(minutes=20)  # only 20m post available < 60m
    c = build_signal_analysis_contract(
        signal_id="ns_late",
        parent_fight_event_id="P",
        profile_id="p",
        profile_basis="VOLUME",
        profile_start_ts=t0 - timedelta(minutes=30),
        profile_end_ts=t0 - timedelta(minutes=1),
        vah=100,
        val=99,
        poc=99.5,
        edge="TPO_VAH",
        edge_price=100,
        trigger_ts=t0,
        trigger_price=99.9,
        continuity_epoch_id=0,
        capture_available_until=hard,
    )
    assert c.research_eligible is False
    assert REASON_INSUFFICIENT_SIGNAL_POST_COVERAGE in c.research_ineligible_reasons


def test_profiles_not_overwritten_across_signals(tmp_path: Path):
    fr = FullObEdgeFlightRecorder(_settings(tmp_path), full_book_manager=_fake())
    t0 = datetime(2026, 9, 4, 8, 5, tzinfo=timezone.utc)
    _start_cross(fr, t0)
    plan = fr._plans["BTCUSDT"]
    assert plan.signal_analysis_contracts
    parent_prof = plan.signal_analysis_contracts[0]["profile_id"]
    sig = NestedSignalRecord(
        nested_signal_contract=NESTED_SIGNAL_CONTRACT,
        nested_signal_id="ns_iso_1",
        parent_fight_event_id=plan.fight_event_id,
        continuity_epoch_id=0,
        parent_segment_index=0,
        symbol="BTCUSDT",
        signal_ts=(t0 + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        receive_time_ns=1,
        profile_id="nested_prof_xyz",
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
        cross_ts=(t0 + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        arm_cycle_id=1,
        causal_cutoff_ts="2026-09-04T09:00:00Z",
        capture_status="PARENT_CAPTURE_OPEN",
        signal_capture_continuous=True,
        signal_research_eligible=True,
        dedup_key="k1",
        prior_zone_state="APPROACH",
        trigger_zone_state="IN",
    )
    fr._emit_nested_signal("BTCUSDT", sig, plan, t0 + timedelta(hours=1))
    assert len(plan.signal_analysis_contracts) == 2
    nested = [c for c in plan.signal_analysis_contracts if c["signal_kind"] == "NESTED"][0]
    parent = [c for c in plan.signal_analysis_contracts if c["signal_kind"] == "PARENT"][0]
    assert parent["profile_id"] == parent_prof
    assert nested["profile_id"] == "nested_prof_xyz"
    assert nested["edge_price"] == 101.0
    assert parent["edge_price"] != nested["edge_price"] or parent["signal_id"] != nested["signal_id"]
    assert list(fr._writers) == ["BTCUSDT"]
    assert fr._plans["BTCUSDT"].fight_event_id == plan.fight_event_id
    root = fr._event_root_for(fr._writers["BTCUSDT"])
    assert (root / "signal_analysis_contracts.jsonl").exists()
    fr.shutdown()


def test_no_second_writer_parent_or_raw(tmp_path: Path):
    fr = FullObEdgeFlightRecorder(_settings(tmp_path), full_book_manager=_fake())
    t0 = datetime(2026, 9, 4, 8, 5, tzinfo=timezone.utc)
    _start_cross(fr, t0)
    plan = fr._plans["BTCUSDT"]
    eid = plan.fight_event_id
    for i in range(3):
        sig = NestedSignalRecord(
            nested_signal_contract=NESTED_SIGNAL_CONTRACT,
            nested_signal_id=f"ns_{i}",
            parent_fight_event_id=eid,
            continuity_epoch_id=0,
            parent_segment_index=0,
            symbol="BTCUSDT",
            signal_ts=(t0 + timedelta(minutes=30 * (i + 1))).isoformat().replace("+00:00", "Z"),
            receive_time_ns=i,
            profile_id=f"prof_{i}",
            profile_basis="VOLUME",
            profile_window_minutes=30,
            profile_start_ts="2026-09-04T08:00:00Z",
            profile_end_ts="2026-09-04T08:30:00Z",
            profile_calculation_version="1",
            profile_fallback_used=True,
            true_tpo_computed=False,
            vah=100.0 + i,
            val=99.0,
            poc=99.5,
            edge="TPO_VAH",
            edge_side="UPPER",
            edge_price=100.0 + i,
            trigger_price=99.9,
            distance_bps=5.0,
            arm_threshold_bps=50.0,
            entry_threshold_bps=20.0,
            rearm_threshold_bps=75.0,
            arm_ts=None,
            cross_ts=(t0 + timedelta(minutes=30 * (i + 1))).isoformat().replace("+00:00", "Z"),
            arm_cycle_id=1,
            causal_cutoff_ts="2026-09-04T08:30:00Z",
            capture_status="PARENT_CAPTURE_OPEN",
            signal_capture_continuous=True,
            signal_research_eligible=True,
            dedup_key=f"k{i}",
            prior_zone_state="APPROACH",
            trigger_zone_state="IN",
        )
        fr._emit_nested_signal("BTCUSDT", sig, plan, t0 + timedelta(minutes=30 * (i + 1)))
    assert list(fr._writers) == ["BTCUSDT"]
    assert fr._plans["BTCUSDT"].fight_event_id == eid
    assert plan.nested_signal_count == 3
    fr.shutdown()


def test_clickhouse_roundtrip_and_idempotent_import():
    t0 = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
    contracts = [_mk("sigA", t0), _mk("sigB", t0 + timedelta(minutes=15))]
    rows = clickhouse_roundtrip_rows(contracts)
    assert len(rows) == 2
    assert {r["signal_id"] for r in rows} == {"sigA", "sigB"}
    store: dict = {}
    r1 = idempotent_merge_import(store, rows)
    r2 = idempotent_merge_import(store, rows)
    assert r1["inserted"] == 2
    assert r2["skipped_identical"] == 2
    assert r2["total"] == 2


def test_statistical_export_marks_overlap_non_independent():
    t0 = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
    clustered = assign_overlap_clusters([_mk("sigA", t0), _mk("sigB", t0 + timedelta(minutes=10))])
    store = SignalMetricStore()
    store.set_metric("sigA", "x", 1)
    store.set_metric("sigB", "x", 2)
    cases = store.export_statistical_cases(clustered)
    assert all(c["independent_observation"] is False for c in cases)
    assert ANALYSIS_ISOLATION_CONTRACT

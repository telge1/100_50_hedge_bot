"""Tests for full_ob_resync_checkpoint_v1 contract."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orderbook_analyse.orderbook_v2_live.full_book_state import FullBookState
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.async_sink import (
    NonBlockingDeltaSink,
)
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.capture_plan import CapturePlan
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.continuity_contract import (
    RECORD_BOOK_DELTA,
    RECORD_INITIAL_CHECKPOINT,
    RECORD_RESYNC_BOUNDARY,
    RECORD_RESYNC_CHECKPOINT,
    annotate_delta_record,
    book_content_hash,
    build_checkpoint_record,
    build_resync_boundary_record,
)
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.event_writer import (
    ActiveEventWriter,
)
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.manager import (
    FullObEdgeFlightRecorder,
)
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.replay import (
    replay_event_directory,
)
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.config import (
    FlightRecorderSettings,
)


def _snap(u: int, seq: int, *, n_bids: int = 3, n_asks: int = 3) -> dict:
    bids = [[str(100.0 - i * 0.1), str(1.0 + i)] for i in range(n_bids)]
    asks = [[str(100.1 + i * 0.1), str(1.0 + i)] for i in range(n_asks)]
    return {"s": "BTCUSDT", "b": bids, "a": asks, "u": u, "seq": seq, "ts": 1_000_000 + u, "cts": 1_000_000 + u}


def _delta(u: int, seq: int, *, phase: str = "live", outcome: str = "applied") -> dict:
    return {
        "topic": "orderbook.full.BTCUSDT",
        "type": "delta",
        "ts": 1_000_000 + u,
        "cts": 1_000_000 + u,
        "local_receive_time_ns": 1_000_000_000 + u,
        "flight_phase": phase,
        "apply_outcome": outcome,
        "data": {"s": "BTCUSDT", "b": [["99.9", "1"]], "a": [["100.2", "1"]], "u": u, "seq": seq},
        "level_update_count": 2,
    }


def _plan(event_id: str = "BTCUSDT_TEST") -> CapturePlan:
    now = datetime.now(timezone.utc)
    return CapturePlan(
        fight_event_id=event_id,
        symbol_event_id=event_id,
        symbol="BTCUSDT",
        trigger_ts=now,
        trigger_receive_time_ns=1,
        trigger_u=1,
        trigger_seq=1,
        trigger_source="CROSS_IN",
        edge="TPO_VAL",
        edge_type="LOWER",
        edge_price=100.0,
        edge_price_at_trigger=100.0,
        profile_session_start=None,
        profile_cutoff_ts=None,
        profile_contract_version=None,
        market_price_at_trigger=99.0,
        distance_to_edge_bps=10.0,
        prior_zone_state="APPROACH",
        trigger_zone_state="IN",
        edge_entry_crossed=True,
        bootstrap_status="N/A",
        dedup_key="x",
        minimum_capture_end_ts=now,
        hard_capture_end_ts=now,
        normal_end_ts=now,
    )


def test_historical_gap_regression_two_epochs(tmp_path: Path) -> None:
    """u 4350204 → reconnect → checkpoint → u 4350353."""
    event_id = "BTCUSDT_20260904T080534Z_1fd9a66d36"
    directory = tmp_path / event_id
    directory.mkdir()
    snap0 = _snap(4350204, 805051079937, n_bids=20, n_asks=20)
    (directory / "rest_full_snapshot.json.zst").write_bytes(__import__("zstandard").ZstdCompressor().compress(__import__("orjson").dumps(snap0)))
    writer = ActiveEventWriter(
        event_id=event_id,
        symbol="BTCUSDT",
        directory=directory,
        started_at=datetime.now(timezone.utc),
        trigger_reason="test",
        trigger_meta={},
        profile_context={},
        config_snapshot={},
        fight_event_id=event_id,
    )
    writer.open()
    writer.write_rest_snapshot(snap0)
    sink = NonBlockingDeltaSink(writer, queue_size=1024, batch_max_messages=32, batch_max_bytes=1 << 20)
    # epoch 0 checkpoint + last deltas ending at 4350204
    ck0 = build_checkpoint_record(
        record_kind=RECORD_INITIAL_CHECKPOINT,
        fight_event_id=event_id,
        continuity_epoch_id=0,
        record_ordinal=1,
        symbol="BTCUSDT",
        topic="orderbook.full.BTCUSDT",
        snapshot=snap0,
        receive_time_ns=1,
        segment_index=0,
    )
    assert sink.try_put(ck0)
    for u in range(4350200, 4350205):
        rec = annotate_delta_record(
            _delta(u, 8000 + u),
            fight_event_id=event_id,
            continuity_epoch_id=0,
            record_ordinal=u,
            segment_index=0,
        )
        assert sink.try_put(rec)
    boundary = build_resync_boundary_record(
        fight_event_id=event_id,
        continuity_epoch_id=0,
        record_ordinal=100,
        symbol="BTCUSDT",
        segment_index=0,
        reason="stale_market_data",
        prev_u=4350204,
        prev_seq=805051079937,
        prev_exchange_ts_ms=1788509523272,
        prev_receive_time_ns=1788509523362596236,
        disconnect_ts_iso="2026-09-04T08:12:03Z",
        reconnect_ts_iso="2026-09-04T08:12:17Z",
        receive_time_ns=2,
    )
    assert sink.try_put(boundary)
    snap1 = _snap(4350353, 805051320556, n_bids=25, n_asks=22)
    ck1 = build_checkpoint_record(
        record_kind=RECORD_RESYNC_CHECKPOINT,
        fight_event_id=event_id,
        continuity_epoch_id=1,
        record_ordinal=101,
        symbol="BTCUSDT",
        topic="orderbook.full.BTCUSDT",
        snapshot=snap1,
        receive_time_ns=3,
        segment_index=0,
        resync_reason="stale_market_data",
        prev_u=4350204,
        prev_seq=805051079937,
    )
    assert sink.try_put(ck1)
    for u in range(4350353, 4350358):
        rec = annotate_delta_record(
            _delta(u, 9000 + u, phase="live"),
            fight_event_id=event_id,
            continuity_epoch_id=1,
            record_ordinal=200 + u,
            segment_index=0,
        )
        # first after checkpoint should be u+1 from baseline when baseline is snap u
        if u == 4350353:
            # apply would be IGNORED_DUP against snap u — use next
            continue
        assert sink.try_put(rec)
    # wait drain
    deadline = time.time() + 5
    while sink.backlog > 0 and time.time() < deadline:
        time.sleep(0.01)
    sink.stop()
    writer.finalize(ended_at=datetime.now(timezone.utc), status="TEST", report_md="# test\n")
    (directory / "manifest.json").write_text(json.dumps({"symbol": "BTCUSDT", "sha256": {}}))
    (directory / "event_manifest.json").write_text(
        json.dumps(
            {
                "trigger_quality": "REAL_CROSS_IN",
                "continuous_capture": False,
                "replayable_by_epochs": True,
                "research_eligible": False,
            }
        )
    )
    result = replay_event_directory(directory)
    assert result["replayable_by_epochs"] is True
    assert result["continuous_capture"] is False
    assert result["research_eligible"] is False
    assert result["continuity_epoch_count"] == 2
    assert result["epochs"][0]["end_u"] == 4350204
    assert result["epochs"][1]["start_u"] == 4350353
    assert result["unobserved_intervals"]
    # Must not invent u+1 across gap
    assert result["epochs"][0]["end_u"] + 1 != result["epochs"][1]["start_u"]


def test_checkpoint_hash_tamper_fails(tmp_path: Path) -> None:
    event_id = "TAMPER"
    directory = tmp_path / event_id
    directory.mkdir()
    snap0 = _snap(10, 100)
    import orjson
    import zstandard as zstd

    (directory / "rest_full_snapshot.json.zst").write_bytes(zstd.ZstdCompressor().compress(orjson.dumps(snap0)))
    writer = ActiveEventWriter(
        event_id=event_id,
        symbol="BTCUSDT",
        directory=directory,
        started_at=datetime.now(timezone.utc),
        trigger_reason="t",
        trigger_meta={},
        profile_context={},
        config_snapshot={},
        fight_event_id=event_id,
    )
    writer.open()
    sink = NonBlockingDeltaSink(writer, queue_size=64)
    ck = build_checkpoint_record(
        record_kind=RECORD_INITIAL_CHECKPOINT,
        fight_event_id=event_id,
        continuity_epoch_id=0,
        record_ordinal=1,
        symbol="BTCUSDT",
        topic="orderbook.full.BTCUSDT",
        snapshot=snap0,
        receive_time_ns=1,
        segment_index=0,
    )
    ck["book_hash"] = "0" * 64  # tamper
    assert sink.try_put(ck)
    time.sleep(0.2)
    sink.stop()
    writer.finalize(ended_at=datetime.now(timezone.utc), status="TEST", report_md="# test\n")
    (directory / "manifest.json").write_text(json.dumps({"symbol": "BTCUSDT", "sha256": {}}))
    result = replay_event_directory(directory)
    assert result["ok"] is False
    assert result["status"] == "CHECKPOINT_HASH_MISMATCH"


def test_queue_full_checkpoint_fail_closed() -> None:
    fr = FullObEdgeFlightRecorder(settings=FlightRecorderSettings(enabled=True, symbols=frozenset({"BTCUSDT"})))
    plan = _plan()
    fr._plans["BTCUSDT"] = plan
    import tempfile

    d = Path(tempfile.mkdtemp())
    writer = ActiveEventWriter(
        event_id=plan.fight_event_id,
        symbol="BTCUSDT",
        directory=d,
        started_at=datetime.now(timezone.utc),
        trigger_reason="t",
        trigger_meta={},
        profile_context={},
        config_snapshot={},
        fight_event_id=plan.fight_event_id,
    )
    writer.open()
    sink = NonBlockingDeltaSink(writer, queue_size=64, batch_max_messages=1)
    fr._sinks["BTCUSDT"] = sink
    fr._writers["BTCUSDT"] = writer
    fr._awaiting_resync_checkpoint["BTCUSDT"] = True
    fr._continuity_epoch["BTCUSDT"] = 0
    fr._epoch_prev_meta["BTCUSDT"] = {"reason": "stale_market_data", "prev_u": 1}
    sink.try_put = lambda _r: False  # type: ignore[method-assign]
    huge = _snap(2, 3, n_bids=100, n_asks=100)
    fr._handle_resync_ready(
        symbol="BTCUSDT",
        payload={"topic": "orderbook.full.BTCUSDT", "data": huge},
        receive_time_ns=1,
    )
    assert plan.checkpoint_persist_failed is True
    assert plan.research_eligible is False
    assert fr._awaiting_resync_checkpoint.get("BTCUSDT") is True
    sink.stop()


def test_markers_iso_excluded_from_u_continuity(tmp_path: Path) -> None:
    writer = ActiveEventWriter(
        event_id="M",
        symbol="BTCUSDT",
        directory=tmp_path,
        started_at=datetime.now(timezone.utc),
        trigger_reason="t",
        trigger_meta={},
        profile_context={},
        config_snapshot={},
        fight_event_id="M",
    )
    writer.open()
    writer._note_continuity(
        {
            "channel": "marker",
            "marker_type": "EDGE_RETOUCH",
            "ts": "2026-09-04T08:12:03.515409Z",
            "record_kind": "EVENT_MARKER",
        }
    )
    writer._note_continuity(
        annotate_delta_record(_delta(5, 50), fight_event_id="M", continuity_epoch_id=0, record_ordinal=1, segment_index=0)
    )
    writer._note_continuity(
        annotate_delta_record(_delta(6, 51), fight_event_id="M", continuity_epoch_id=0, record_ordinal=2, segment_index=0)
    )
    assert writer.persisted_u_gap_count == 0
    writer.finalize(ended_at=datetime.now(timezone.utc), status="OK", report_md="# ok\n")


def test_research_eligibility_matrix() -> None:
    p = _plan()
    p.recompute_research_flags()
    assert p.continuous_capture is True
    assert p.research_eligible is True
    p.transport_reconnect_count = 1
    p.unobserved_interval_count = 1
    p.recompute_research_flags()
    assert p.continuous_capture is False
    assert p.research_eligible is False
    assert p.replayable_by_epochs is True  # still true until checkpoint failure


def test_book_hash_stable() -> None:
    s = _snap(1, 2)
    assert book_content_hash(bids=s["b"], asks=s["a"]) == book_content_hash(bids=list(reversed(s["b"])), asks=s["a"])


def test_large_checkpoint_copy_lock_budget() -> None:
    """~60k levels: copy under lock budget soft-check (<50ms typical)."""
    n = 30_000
    snap = _snap(1, 1, n_bids=n, n_asks=n)
    book = FullBookState("BTCUSDT")
    t0 = time.perf_counter()
    book.apply_snapshot(bids=snap["b"], asks=snap["a"], u=1, seq=1, ts_ms=1, mark_ready=True)
    cons = book.copy_consistent_snapshot()
    dt_ms = (time.perf_counter() - t0) * 1000
    assert len(cons.bids) == n
    assert len(cons.asks) == n
    assert cons.best_bid() < cons.best_ask()
    # Soft budget — CI variance allowed
    assert dt_ms < 5_000


def test_reconnect_without_open_event_clears_prebuffer() -> None:
    fr = FullObEdgeFlightRecorder(settings=FlightRecorderSettings(enabled=True, symbols=frozenset({"BTCUSDT"})))
    buf = fr._buf("BTCUSDT")
    buf.append(_delta(1, 1), kind="delta", receive_time_ns=1)
    assert len(buf) == 1
    fr._handle_reconnect_phase(
        symbol="BTCUSDT",
        payload={"reason": "stale_market_data", "prev_u": 1},
        receive_time_ns=2,
        received_at=datetime.now(timezone.utc),
    )
    assert len(buf) == 0
    assert fr.watcher.state("BTCUSDT").pre_trigger_incomplete is True


def test_multi_reconnect_epochs_in_manager_gate() -> None:
    fr = FullObEdgeFlightRecorder(settings=FlightRecorderSettings(enabled=True, symbols=frozenset({"BTCUSDT"})))
    plan = _plan()
    fr._plans["BTCUSDT"] = plan
    import tempfile

    d = Path(tempfile.mkdtemp())
    writer = ActiveEventWriter(
        event_id=plan.fight_event_id,
        symbol="BTCUSDT",
        directory=d,
        started_at=datetime.now(timezone.utc),
        trigger_reason="t",
        trigger_meta={},
        profile_context={},
        config_snapshot={},
        fight_event_id=plan.fight_event_id,
    )
    writer.open()
    sink = NonBlockingDeltaSink(writer, queue_size=4096, batch_max_bytes=1 << 22)
    fr._sinks["BTCUSDT"] = sink
    fr._writers["BTCUSDT"] = writer
    fr._continuity_epoch["BTCUSDT"] = 0
    # first reconnect
    fr.on_full_ob_message(
        symbol="BTCUSDT",
        payload={"reason": "stale_market_data", "prev_u": 10, "prev_seq": 1, "reconnect_count": 1},
        received_at=datetime.now(timezone.utc),
        receive_time_ns=1,
        phase="reconnect",
    )
    assert fr._awaiting_resync_checkpoint["BTCUSDT"] is True
    # delta while awaiting must be held
    fr.on_full_ob_message(
        symbol="BTCUSDT",
        payload={"topic": "orderbook.full.BTCUSDT", "type": "delta", "data": {"s": "BTCUSDT", "b": [["1", "1"]], "a": [["2", "1"]], "u": 20, "seq": 2}},
        received_at=datetime.now(timezone.utc),
        receive_time_ns=2,
        phase="buffer",
        outcome="accepted",
    )
    assert len(fr._held_pre_checkpoint.get("BTCUSDT") or []) == 1
    fr.on_full_ob_message(
        symbol="BTCUSDT",
        payload={"topic": "orderbook.full.BTCUSDT", "data": _snap(20, 2)},
        received_at=datetime.now(timezone.utc),
        receive_time_ns=3,
        phase="resync_ready",
        outcome="checkpoint",
    )
    assert fr._awaiting_resync_checkpoint["BTCUSDT"] is False
    assert fr._continuity_epoch["BTCUSDT"] == 1
    assert plan.resync_checkpoint_success_count >= 1
    assert plan.continuous_capture is False
    assert plan.research_eligible is False
    deadline = time.time() + 3
    while sink.backlog > 0 and time.time() < deadline:
        time.sleep(0.01)
    sink.stop()
    writer.finalize(ended_at=datetime.now(timezone.utc), status="OK", report_md="# ok\n")

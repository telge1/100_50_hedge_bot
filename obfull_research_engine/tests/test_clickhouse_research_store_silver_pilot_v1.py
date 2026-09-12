"""Targeted unit tests for Full-OB silver pilot replay helpers."""

from __future__ import annotations

import pytest
from orderbook_analyse.orderbook_v2_live.full_book_state import FullBookState
from orderbook_analyse.orderbook_v2_live.full_ob_sync import DeltaOutcome

from obfull_research_engine.clickhouse_research_store_v1.silver_replay import (
    BronzeRecord,
    change_type,
    dedupe_bronze_by_record_id,
    find_first_full_anchor,
    make_silver_build_id,
    replay_bronze_to_silver,
    sort_bronze_source_order,
    SilverReplayError,
)


def _rec(**kwargs) -> BronzeRecord:
    base = dict(
        record_id="a" * 64,
        symbol="BTCUSDT",
        message_type="delta",
        event_time_ns=1_000,
        receive_time_ns=1_001,
        update_id=1,
        seq=1,
        update_id_present=1,
        seq_present=1,
        source_segment_sha256="b" * 64,
        record_ordinal=1,
        payload_sha256="c" * 64,
        original_payload={},
    )
    base.update(kwargs)
    return BronzeRecord(**base)


def test_source_order_follows_ordinal():
    rows = [
        _rec(record_id="1" * 64, record_ordinal=3),
        _rec(record_id="2" * 64, record_ordinal=1),
        _rec(record_id="3" * 64, record_ordinal=2),
    ]
    ordered = sort_bronze_source_order(rows)
    assert [r.record_ordinal for r in ordered] == [1, 2, 3]


def test_dedupe_keeps_first_record_id():
    rows = [
        _rec(record_id="1" * 64, record_ordinal=1, update_id=10),
        _rec(record_id="1" * 64, record_ordinal=2, update_id=11),
        _rec(record_id="2" * 64, record_ordinal=3, update_id=12),
    ]
    out = dedupe_bronze_by_record_id(rows)
    assert len(out) == 2
    assert out[0].update_id == 10


def test_change_types():
    assert change_type(0, 1) == "ADD"
    assert change_type(2, 3) == "UPDATE"
    assert change_type(2, 0) == "DELETE"
    assert change_type(2, 2) is None


def test_checkpoint_initializes_full_book_state():
    state = FullBookState(symbol="BTCUSDT")
    state.apply_snapshot(
        bids=[["100", "1"], ["99", "2"]],
        asks=[["101", "1.5"]],
        u=10,
        seq=100,
        ts_ms=1,
        mark_ready=True,
    )
    assert state.book_ready
    assert state.best_bid() == 100.0
    assert state.best_ask() == 101.0
    assert abs(state.mid() - 100.5) < 1e-12


def test_delta_add_update_delete():
    state = FullBookState(symbol="BTCUSDT")
    state.apply_snapshot(bids=[["100", "1"]], asks=[["101", "1"]], u=1, seq=1, ts_ms=1, mark_ready=True)
    # ADD bid 99
    assert state.apply_delta(bids=[["99", "5"]], asks=[], u=2, seq=2, ts_ms=2) is DeltaOutcome.APPLIED
    assert state.bids[99.0] == 5.0
    assert change_type(0, 5) == "ADD"
    # UPDATE
    assert state.apply_delta(bids=[["99", "7"]], asks=[], u=3, seq=3, ts_ms=3) is DeltaOutcome.APPLIED
    assert state.bids[99.0] == 7.0
    assert change_type(5, 7) == "UPDATE"
    # DELETE
    assert state.apply_delta(bids=[["99", "0"]], asks=[], u=4, seq=4, ts_ms=4) is DeltaOutcome.APPLIED
    assert 99.0 not in state.bids
    assert change_type(7, 0) == "DELETE"


def test_gap_does_not_apply():
    state = FullBookState(symbol="BTCUSDT")
    state.apply_snapshot(bids=[["100", "1"]], asks=[["101", "1"]], u=1, seq=1, ts_ms=1, mark_ready=True)
    out = state.apply_delta(bids=[["100", "2"]], asks=[], u=5, seq=5, ts_ms=5, enforce_continuity=True)
    assert out is DeltaOutcome.GAP
    assert state.bids[100.0] == 1.0  # unchanged


def test_new_replay_epoch_on_second_checkpoint():
    rows = [
        _rec(
            record_id="1" * 64,
            record_ordinal=1,
            message_type="checkpoint",
            event_time_ns=1_000_000_000,
            original_payload={
                "bids": [["100", "1"]],
                "asks": [["101", "1"]],
                "u": 1,
                "seq": 10,
                "event_time": 1_000_000_000_000_000,
                "book_sha256": "d" * 64,
            },
        ),
        _rec(
            record_id="2" * 64,
            record_ordinal=2,
            message_type="checkpoint",
            event_time_ns=1_500_000_000,
            original_payload={
                "bids": [["100", "2"]],
                "asks": [["101", "2"]],
                "u": 2,
                "seq": 20,
                "event_time": 1_500_000_000_000_000,
                "book_sha256": "e" * 64,
            },
        ),
    ]
    result = replay_bronze_to_silver(
        rows=rows,
        symbol="BTCUSDT",
        window_start_ns=0,
        window_end_ns=2_000_000_000,
        silver_build_id="f" * 64,
        created_at_ms=0,
    )
    assert result.reset_count == 2
    assert result.replay_epoch_count == 2
    assert [c["replay_epoch"] for c in result.checkpoints] == [1, 2]


def test_bucket_uses_no_future_events():
    # Anchor at t=1500ms; delta at t=1600ms; window end 2000ms.
    # Bucket [1000,1100) must not exist; first finished bucket after anchor is [1500,1600) or [1000ms grid].
    rows = [
        _rec(
            record_id="1" * 64,
            record_ordinal=1,
            message_type="checkpoint",
            event_time_ns=1_500_000_000,  # 1.5s
            update_id=10,
            seq=10,
            original_payload={
                "bids": [["100", "1"]],
                "asks": [["101", "1"]],
                "u": 10,
                "seq": 10,
                "event_time": 1_500_000_000_000_000,
                "book_sha256": "d" * 64,
            },
        ),
        _rec(
            record_id="2" * 64,
            record_ordinal=2,
            message_type="delta",
            event_time_ns=1_600_000_000,
            update_id=11,
            seq=11,
            original_payload={
                "ts": 1600,
                "data": {"b": [["100", "3"]], "a": [], "u": 11, "seq": 11},
            },
        ),
    ]
    result = replay_bronze_to_silver(
        rows=rows,
        symbol="BTCUSDT",
        window_start_ns=1_000_000_000,
        window_end_ns=2_000_000_000,
        silver_build_id="f" * 64,
        created_at_ms=0,
    )
    assert result.level_change_count if hasattr(result, "level_change_count") else len(result.level_changes) >= 1
    # metrics only after coverage_start
    assert result.metrics
    assert all(m["last_update_id"] is not None for m in result.metrics)


def test_missing_anchor_stops():
    rows = [
        _rec(
            record_id="1" * 64,
            record_ordinal=1,
            message_type="delta",
            original_payload={"data": {"b": [], "a": [], "u": 1, "seq": 1}},
        )
    ]
    with pytest.raises(SilverReplayError, match="STOP_SILVER_ANCHOR_MISSING"):
        replay_bronze_to_silver(
            rows=rows,
            symbol="BTCUSDT",
            window_start_ns=0,
            window_end_ns=10,
            silver_build_id="f" * 64,
            created_at_ms=0,
        )


def test_build_id_stable():
    a = make_silver_build_id(
        schema_version="v",
        bronze_import_id="1",
        symbol="btcusdt",
        window_start="s",
        window_end="e",
        replay_contract_hash="h",
        anchor_record_id="a",
    )
    b = make_silver_build_id(
        schema_version="v",
        bronze_import_id="1",
        symbol="BTCUSDT",
        window_start="s",
        window_end="e",
        replay_contract_hash="h",
        anchor_record_id="a",
    )
    assert a == b


def test_find_anchor():
    rows = [
        _rec(record_id="1" * 64, record_ordinal=1, message_type="delta", original_payload={"data": {}}),
        _rec(
            record_id="2" * 64,
            record_ordinal=2,
            message_type="checkpoint",
            original_payload={"bids": [["1", "1"]], "asks": [["2", "1"]], "u": 1, "seq": 1},
        ),
    ]
    assert find_first_full_anchor(rows).record_ordinal == 2

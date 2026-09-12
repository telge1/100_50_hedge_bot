"""Tests for checkpoint reanchor + replay order audit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from obfull_research_engine.clickhouse_research_store_v1.reanchor_replay_order_audit import (
    CHECKPOINT_PROVENANCE,
    _classify_u_jump,
    audit_out_of_order_event_time,
    compute_epochs_and_safe_intervals_v2,
    replay_with_order,
)
from obfull_research_engine.clickhouse_research_store_v1.gap_semantics_audit import iter_records_stream


def test_periodic_checkpoint_not_independent():
    assert CHECKPOINT_PROVENANCE["periodic_5m"]["may_open_epoch_after_gap"] is False


def test_classify_stale_u_as_false_alarm():
    cls, _ = _classify_u_jump(prev_u=100, cur_u=98, prev_seq=10, cur_seq=11, next_anchor=None)
    assert cls == "FALSE_SEQUENCE_ALARM"


def test_classify_recovered_by_exchange_snapshot():
    cls, _ = _classify_u_jump(
        prev_u=100,
        cur_u=105,
        prev_seq=10,
        cur_seq=15,
        next_anchor={"kind": "exchange_snapshot"},
    )
    assert cls == "RECOVERED_BY_INDEPENDENT_EXCHANGE_SNAPSHOT"


def test_out_of_order_event_time_stream_continuous():
    records = [
        ({"message_type": "checkpoint", "event_time_ns": 1_000, "original_payload": {"u": 1, "bids": [["1", "1"]], "asks": [["2", "1"]]}}, 1),
        ({"message_type": "delta", "event_time_ns": 3_000, "original_payload": {"data": {"u": 2, "b": [], "a": []}}}, 2),
        ({"message_type": "delta", "event_time_ns": 2_000, "original_payload": {"data": {"u": 2, "b": [], "a": []}}}, 3),
    ]
    events = audit_out_of_order_event_time(records)
    assert len(events) == 1
    assert events[0]["classification"] == "OUT_OF_ORDER_BUT_STREAM_CONTINUOUS"


def test_replay_physical_vs_event_time_differs(tmp_path: Path):
    import zstandard as zstd

    records = [
        {
            "message_type": "checkpoint",
            "event_time_ns": 1_000_000_000,
            "receive_time_ns": 1_000_000_000,
            "original_payload": {
                "checkpoint_reason": "segment_start",
                "u": 10,
                "bids": [["100.0", "1.0"]],
                "asks": [["101.0", "1.0"]],
            },
        },
        {
            "message_type": "delta",
            "event_time_ns": 3_000_000_000,
            "receive_time_ns": 3_000_000_000,
            "original_payload": {"data": {"u": 11, "b": [["100.0", "2.0"]], "a": []}},
        },
        {
            "message_type": "delta",
            "event_time_ns": 2_000_000_000,
            "receive_time_ns": 2_000_000_000,
            "original_payload": {"data": {"u": 12, "b": [["100.0", "3.0"]], "a": []}},
        },
    ]
    seg = tmp_path / "t.ndjson.zst"
    raw = b"".join(json.dumps(r, sort_keys=True).encode() + b"\n" for r in records)
    seg.write_bytes(zstd.ZstdCompressor(level=1).compress(raw))
    loaded = list(iter_records_stream(seg))
    phys = replay_with_order(loaded, order_key="physical_ordinal")
    evt = replay_with_order(loaded, order_key="event_time_ordinal")
    assert phys["book_sha256"] != evt["book_sha256"]


def test_epoch_not_opened_by_periodic_after_gap(tmp_path: Path):
    import zstandard as zstd

    records = [
        {
            "message_type": "checkpoint",
            "event_time_ns": 1_000_000_000,
            "receive_time_ns": 1_000_000_000,
            "original_payload": {
                "checkpoint_reason": "segment_start",
                "u": 10,
                "bids": [["100.0", "1.0"]],
                "asks": [["101.0", "1.0"]],
            },
        },
        {
            "message_type": "gap_marker",
            "event_time_ns": 2_000_000_000,
            "receive_time_ns": 2_000_000_000,
            "original_payload": {"details": {"reason": "stale_market_data"}},
        },
        {
            "message_type": "checkpoint",
            "event_time_ns": 3_000_000_000,
            "receive_time_ns": 3_000_000_000,
            "original_payload": {
                "checkpoint_reason": "periodic_5m",
                "u": 20,
                "bids": [["100.0", "1.0"]],
                "asks": [["101.0", "1.0"]],
            },
        },
    ]
    seg = tmp_path / "g.ndjson.zst"
    raw = b"".join(json.dumps(r, sort_keys=True).encode() + b"\n" for r in records)
    seg.write_bytes(zstd.ZstdCompressor(level=1).compress(raw))
    man = {
        "utc_hour": "2026-01-01T00:00:00Z",
        "first_event_time": "2026-01-01T00:00:00Z",
        "last_event_time": "2026-01-01T00:00:10Z",
    }
    (tmp_path / "g.ndjson.zst.manifest.json").write_text(json.dumps(man))
    epochs, safe, stats = compute_epochs_and_safe_intervals_v2(seg, man)
    assert len(epochs) == 1
    assert epochs[0].anchor_type == "segment_start_clean"

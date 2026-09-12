"""Isolated tests for Full-OB gap semantics audit."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from obfull_research_engine.clickhouse_research_store_v1.gap_semantics_audit import (
    audit_segment,
    classify_gap_event,
    apply_warmup,
    iter_records_stream,
)


def _write_segment(tmp_path: Path, name: str, records: list[dict], manifest: dict) -> tuple[Path, Path]:
    import zstandard as zstd

    seg = tmp_path / name
    seg.parent.mkdir(parents=True, exist_ok=True)
    raw = b"".join(json.dumps(r, sort_keys=True).encode() + b"\n" for r in records)
    with seg.open("wb") as fh:
        fh.write(zstd.ZstdCompressor(level=1).compress(raw))
    man = tmp_path / f"{name}.manifest.json"
    man.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return seg, man


def _checkpoint(u: int, reason: str = "segment_start", ts_ns: int = 1_000_000_000) -> dict:
    return {
        "message_type": "checkpoint",
        "event_time_ns": ts_ns,
        "receive_time_ns": ts_ns,
        "u": u,
        "original_payload": {
            "checkpoint_reason": reason,
            "u": u,
            "bids": [["100.0", "1.0"]],
            "asks": [["101.0", "1.0"]],
        },
    }


def _delta(u: int, ts_ns: int) -> dict:
    return {
        "message_type": "delta",
        "event_time_ns": ts_ns,
        "receive_time_ns": ts_ns,
        "u": u,
        "original_payload": {"data": {"u": u, "b": [], "a": []}},
    }


def _gap_marker(reason: str = "transport_reconnect") -> dict:
    return {
        "message_type": "gap_marker",
        "event_time_ns": None,
        "receive_time_ns": 2_000_000_000,
        "original_payload": {"details": {"reason": reason}},
    }


def test_classify_reconnect_with_reanchor():
    recovery = {"validation": {"ok": True}, "reason": "reconnect_resync", "event_time_ns": 3_000_000_000}
    cls, _, _ = classify_gap_event(
        trigger_kind="gap_marker",
        gap_reason="transport_reconnect",
        prev_u=10,
        next_u=None,
        recovery=recovery,
        segment_boundary=False,
        archive_instance_change=False,
        record_ordinal=5,
        in_segment_u_gap=False,
    )
    assert cls == "RECONNECT_WITH_VALID_REANCHOR"


def test_classify_delta_u_jump_recovered():
    recovery = {"validation": {"ok": True}, "reason": "reconnect_resync", "event_time_ns": 3_000_000_000}
    cls, _, _ = classify_gap_event(
        trigger_kind="delta_u_jump",
        gap_reason=None,
        prev_u=10,
        next_u=15,
        recovery=recovery,
        segment_boundary=False,
        archive_instance_change=False,
        record_ordinal=5,
        in_segment_u_gap=True,
    )
    assert cls == "TRUE_MISSING_INTERVAL_RECOVERED"


def test_audit_continuous_segment(tmp_path: Path):
    records = [_checkpoint(100), _delta(101, 1_100_000_000), _delta(102, 1_200_000_000)]
    manifest = {
        "utc_hour": "2026-01-01T00:00:00Z",
        "first_event_time": "2026-01-01T00:00:00Z",
        "last_event_time": "2026-01-01T00:00:01Z",
        "gap_count": 0,
        "completion_status": "COMPLETE",
        "continuity_status": "CONTIGUOUS",
        "archive_instance_id": "arch-a",
    }
    seg, _ = _write_segment(tmp_path, "seg.ndjson.zst", records, manifest)
    result = audit_segment(seg, manifest)
    assert result["delta_u_gaps_found"] == 0
    assert result["gap_markers_found"] == 0
    assert len(result["safe_intervals"]) >= 1


def test_audit_reconnect_reanchor(tmp_path: Path):
    records = [
        _checkpoint(100),
        _delta(101, 1_100_000_000),
        _gap_marker("transport_reconnect"),
        _checkpoint(200, "reconnect_resync", ts_ns=3_000_000_000),
        _delta(201, 2_100_000_000),
    ]
    manifest = {
        "utc_hour": "2026-01-01T01:00:00Z",
        "first_event_time": "2026-01-01T01:00:00Z",
        "last_event_time": "2026-01-01T01:00:05Z",
        "gap_count": 1,
        "completion_status": "GAP",
        "continuity_status": "GAP",
        "archive_instance_id": "arch-a",
    }
    seg, _ = _write_segment(tmp_path, "seg2.ndjson.zst", records, manifest)
    result = audit_segment(seg, manifest)
    assert result["gap_markers_found"] == 1
    assert any(
        g["classification"] in {"RECONNECT_WITH_VALID_REANCHOR", "MANIFEST_FALSE_POSITIVE"}
        for g in result["gaps"]
        if g["trigger_kind"] == "gap_marker"
    )
    assert len(result["safe_intervals"]) >= 2


def test_apply_warmup_splits_short_intervals():
    intervals = [{"start_ns": 0, "end_ns": 120_000_000_000}]
    usable = apply_warmup(intervals, 60)
    assert len(usable) == 1
    assert usable[0]["usable_start_ns"] == 60_000_000_000


def test_iter_records_stream_roundtrip(tmp_path: Path):
    records = [_delta(1, 1_000_000_000), _delta(2, 2_000_000_000)]
    manifest = {"utc_hour": "2026-01-01T00:00:00Z", "gap_count": 0}
    seg, _ = _write_segment(tmp_path, "stream.ndjson.zst", records, manifest)
    out = list(iter_records_stream(seg))
    assert len(out) == 2

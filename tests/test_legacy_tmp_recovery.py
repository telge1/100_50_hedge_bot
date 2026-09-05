"""Tests for legacy FR .tmp recovery (copy-only, fail-closed on open fd)."""

from __future__ import annotations

from pathlib import Path

import orjson
import zstandard as zstd

from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.legacy_tmp_recovery import (
    QUALITY_COMPLETE,
    QUALITY_INCOMPLETE,
    recover_legacy_tmp,
    sha256_file,
)


def _zst_jsonl(records: list[dict], *, incomplete_last_line: bytes | None = None) -> bytes:
    raw = b"".join(orjson.dumps(r) + b"\n" for r in records)
    if incomplete_last_line is not None:
        raw += incomplete_last_line
    return zstd.ZstdCompressor(level=3).compress(raw)


def _snap() -> dict:
    return {
        "s": "BTCUSDT",
        "b": [["100", "2"], ["99", "1"]],
        "a": [["101", "2"], ["102", "1"]],
        "u": 10,
        "seq": 100,
        "ts": 1000,
        "cts": 999,
    }


def _write_snap(path: Path) -> None:
    path.write_bytes(zstd.ZstdCompressor(level=3).compress(orjson.dumps(_snap())))


def _deltas_ok() -> list[dict]:
    return [
        {"type": "delta", "ts": 1000 + u, "data": {"u": u, "seq": 100 + u, "b": [["100", "1"]], "a": []}}
        for u in (11, 12, 13)
    ]


def test_complete_unfinalized_stream(tmp_path: Path):
    src = tmp_path / "full_ob_raw_deltas.jsonl.zst.tmp"
    src.write_bytes(_zst_jsonl(_deltas_ok()))
    snap = tmp_path / "rest_full_snapshot.json.zst.tmp"
    _write_snap(snap)
    before = src.read_bytes()
    res = recover_legacy_tmp(
        original_delta_tmp=src,
        original_snapshot_tmp=snap,
        out_dir=tmp_path / "out",
        symbol="BTCUSDT",
        fight_event_id="e1",
    )
    assert res.blocked is False
    assert res.manifest["data_quality"] == QUALITY_COMPLETE
    assert res.manifest["finalization_reason"] == "INTERRUPTED_BY_LEGACY_COLLECTOR_RESTART"
    assert res.manifest["natural_fight_outcome_complete"] is False
    assert src.read_bytes() == before
    assert sha256_file(src) == res.manifest["original_sha256"]


def test_truncated_zstd_tail(tmp_path: Path):
    src = tmp_path / "full_ob_raw_deltas.jsonl.zst.tmp"
    complete = _zst_jsonl(_deltas_ok())
    src.write_bytes(complete[:-24])
    _write_snap(tmp_path / "rest_full_snapshot.json.zst.tmp")
    before = src.read_bytes()
    res = recover_legacy_tmp(
        original_delta_tmp=src,
        original_snapshot_tmp=tmp_path / "rest_full_snapshot.json.zst.tmp",
        out_dir=tmp_path / "out",
    )
    assert src.read_bytes() == before
    assert res.manifest["data_quality"] == QUALITY_INCOMPLETE
    assert res.manifest["zstd_complete"] is False


def test_truncated_json_line(tmp_path: Path):
    src = tmp_path / "full_ob_raw_deltas.jsonl.zst.tmp"
    src.write_bytes(_zst_jsonl(_deltas_ok(), incomplete_last_line=b'{"type":"delta","data":{"u":'))
    _write_snap(tmp_path / "rest_full_snapshot.json.zst.tmp")
    before = src.read_bytes()
    res = recover_legacy_tmp(
        original_delta_tmp=src,
        original_snapshot_tmp=tmp_path / "rest_full_snapshot.json.zst.tmp",
        out_dir=tmp_path / "out",
    )
    assert src.read_bytes() == before
    assert res.manifest["incomplete_json_tail"] is True
    assert res.manifest["data_quality"] == QUALITY_INCOMPLETE
    assert res.manifest["recovered_record_count"] == 3


def test_u_gap(tmp_path: Path):
    src = tmp_path / "full_ob_raw_deltas.jsonl.zst.tmp"
    recs = [
        {"type": "delta", "ts": 1, "data": {"u": 11, "seq": 111, "b": [], "a": []}},
        {"type": "delta", "ts": 2, "data": {"u": 13, "seq": 113, "b": [], "a": []}},
    ]
    src.write_bytes(_zst_jsonl(recs))
    _write_snap(tmp_path / "rest_full_snapshot.json.zst.tmp")
    res = recover_legacy_tmp(
        original_delta_tmp=src,
        original_snapshot_tmp=tmp_path / "rest_full_snapshot.json.zst.tmp",
        out_dir=tmp_path / "out",
    )
    assert res.manifest["u_gap_count"] >= 1
    assert res.manifest["data_quality"] == QUALITY_INCOMPLETE


def test_stale_updates_still_complete(tmp_path: Path):
    src = tmp_path / "full_ob_raw_deltas.jsonl.zst.tmp"
    recs = _deltas_ok()
    recs.insert(1, {"type": "delta", "ts": 1, "data": {"u": 10, "seq": 99, "b": [], "a": []}})
    src.write_bytes(_zst_jsonl(recs))
    _write_snap(tmp_path / "rest_full_snapshot.json.zst.tmp")
    res = recover_legacy_tmp(
        original_delta_tmp=src,
        original_snapshot_tmp=tmp_path / "rest_full_snapshot.json.zst.tmp",
        out_dir=tmp_path / "out",
    )
    assert res.manifest["stale_or_dup"] >= 1
    assert res.manifest["u_gap_count"] == 0
    assert res.manifest["data_quality"] == QUALITY_COMPLETE


def test_crossed_book(tmp_path: Path):
    src = tmp_path / "full_ob_raw_deltas.jsonl.zst.tmp"
    recs = [{"type": "delta", "ts": 1, "data": {"u": 11, "seq": 111, "b": [["102", "1"]], "a": []}}]
    src.write_bytes(_zst_jsonl(recs))
    _write_snap(tmp_path / "rest_full_snapshot.json.zst.tmp")
    res = recover_legacy_tmp(
        original_delta_tmp=src,
        original_snapshot_tmp=tmp_path / "rest_full_snapshot.json.zst.tmp",
        out_dir=tmp_path / "out",
    )
    assert res.manifest["book_crossed"] is True
    assert res.manifest["data_quality"] == QUALITY_INCOMPLETE


def test_open_fd_blocks_and_original_unchanged(tmp_path: Path):
    src = tmp_path / "full_ob_raw_deltas.jsonl.zst.tmp"
    payload = _zst_jsonl(_deltas_ok())
    src.write_bytes(payload)
    fh = open(src, "ab")
    try:
        res = recover_legacy_tmp(original_delta_tmp=src, out_dir=tmp_path / "out")
        assert res.blocked is True
        assert "open_fd" in (res.block_reason or "")
        assert src.read_bytes() == payload
        assert not (tmp_path / "out" / "recovery_manifest.json").exists()
    finally:
        fh.close()
    res2 = recover_legacy_tmp(original_delta_tmp=src, out_dir=tmp_path / "out2")
    assert res2.blocked is False
    assert src.read_bytes() == payload

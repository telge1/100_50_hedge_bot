"""Targeted unit tests for Full-OB ClickHouse 60s pilot helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from obfull_research_engine.clickhouse_research_store_v1.helpers import (
    ResourceLimits,
    check_resource_limits,
    event_in_window,
    make_import_id,
    make_record_id,
    verify_manifest_against_segment,
)
from obfull_research_engine.clickhouse_research_store_v1.importer import _ledger_status


def test_record_id_deterministic():
    a = make_record_id(source_segment_sha256="abc", record_ordinal=42)
    b = make_record_id(source_segment_sha256="abc", record_ordinal=42)
    assert a == b
    assert a == hashlib.sha256(b"abc:42").hexdigest()
    assert a != make_record_id(source_segment_sha256="abc", record_ordinal=43)


def test_window_half_open_bounds():
    start = 1_000
    end = 2_000
    assert event_in_window(1_000, window_start_ns=start, window_end_ns=end) is True
    assert event_in_window(1_999, window_start_ns=start, window_end_ns=end) is True
    assert event_in_window(2_000, window_start_ns=start, window_end_ns=end) is False
    assert event_in_window(999, window_start_ns=start, window_end_ns=end) is False
    assert event_in_window(None, window_start_ns=start, window_end_ns=end) is False


def test_import_id_stable():
    a = make_import_id(
        schema_version="v1",
        source_segment_sha256="deadbeef",
        symbol="btcusdt",
        window_start="2026-09-06T20:19:00Z",
        window_end="2026-09-06T20:20:00Z",
    )
    b = make_import_id(
        schema_version="v1",
        source_segment_sha256="deadbeef",
        symbol="BTCUSDT",
        window_start="2026-09-06T20:19:00Z",
        window_end="2026-09-06T20:20:00Z",
    )
    assert a == b


def test_resource_guard_deadline():
    limits = ResourceLimits(deadline_monotonic=0.0, max_rss_bytes=10**18)
    with pytest.raises(RuntimeError, match="STOP_RESOURCE_LIMIT"):
        check_resource_limits(limits, now_monotonic=1.0)


def test_resource_guard_callable_rss():
    # Should not raise with huge limit
    limits = ResourceLimits(deadline_monotonic=1e18, max_rss_bytes=10**18)
    check_resource_limits(limits)


def test_manifest_sha_mismatch(tmp_path: Path):
    seg = tmp_path / "x.ndjson.zst"
    seg.write_bytes(b"not-real-but-hashed")
    man = tmp_path / "x.ndjson.zst.manifest.json"
    man.write_text(
        '{"segment_sha256":"' + ("0" * 64) + '","format_version":"full_ob_continuous_raw_archive_v1"}',
        encoding="utf-8",
    )
    # load_manifest path is segment + .manifest.json — write that name
    import json

    from obfull_research_engine.clickhouse_research_store_v1.helpers import load_manifest

    real_man = Path(str(seg) + ".manifest.json")
    real_man.write_text(
        json.dumps({"segment_sha256": "0" * 64, "format_version": "full_ob_continuous_raw_archive_v1"}),
        encoding="utf-8",
    )
    manifest = load_manifest(seg)
    with pytest.raises(RuntimeError, match="STOP_SOURCE_MANIFEST_INVALID"):
        verify_manifest_against_segment(seg, manifest)


def test_complete_import_skipped(monkeypatch):
    """Ledger COMPLETE must yield SKIPPED_ALREADY_COMPLETE without inserts."""
    from obfull_research_engine.clickhouse_research_store_v1 import importer as imp

    class FakeClient:
        def command(self, *_a, **_k):
            return None

        def query(self, sql, parameters=None):
            class R:
                result_rows = [("COMPLETE", 301, "", "2026-09-06 20:00:00.000")]

            if "system.columns" in sql:
                class Empty:
                    result_rows = []

                return Empty()
            if "record_id" in sql and "payload_sha256" in sql:
                class EmptyHash:
                    result_rows = []

                return EmptyHash()
            return R()

        def insert(self, *a, **k):
            raise AssertionError("insert must not be called on COMPLETE skip")

        def close(self):
            return None
    monkeypatch.setattr(imp, "ensure_database_and_tables", lambda _c: None)
    monkeypatch.setattr(imp, "assert_compatible_schema", lambda _c: None)
    monkeypatch.setattr(imp, "verify_manifest_against_segment", lambda *_a, **_k: "a" * 64)
    monkeypatch.setattr(
        imp,
        "load_manifest",
        lambda _p: {
            "format_version": "full_ob_continuous_raw_archive_v1",
            "symbol": "BTCUSDT",
            "segment_sha256": "a" * 64,
        },
    )

    seg = Path("/home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_shadow/full_ob_v1/BTCUSDT/2026/09/06/BTCUSDT_20260906T200000Z_eb3cbb7ffd1f_full_ob_continuous_raw_archive_v1.ndjson.zst")
    result = imp.run_pilot_import(
        segment_path=seg,
        symbol="BTCUSDT",
        window_start="2026-09-06T20:19:00.000000000Z",
        window_end="2026-09-06T20:20:00.000000000Z",
        client=FakeClient(),
    )
    assert result.skipped is True
    assert result.status == "SKIPPED_ALREADY_COMPLETE"
    assert result.rows_inserted == 0


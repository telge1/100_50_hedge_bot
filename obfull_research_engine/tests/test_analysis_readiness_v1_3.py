"""Fail-closed analysis-readiness tests for Silver v1.3."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import obfull_research_engine.clickhouse_research_store_v1.analysis_readiness_v1_3 as readiness
from obfull_research_engine.clickhouse_research_store_v1.epoch_aware_silver_v1_3 import (
    EpochDefinition,
)
from obfull_research_engine.clickhouse_research_store_v1.silver_full_build_v1_3 import (
    BuildConfig,
    plan_epoch_chunks,
)

CHAIN_VERSION = "canonical_segment_chain_v1_3_BTCUSDT_20260912T060011Z_f666e592a0bef459"
CHAIN_HASH = "f666e592a0bef4598545b3f247cf5dd97dd9c53017028e3c33ddfce5e0d15333"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def _epoch(epoch_id: str = "e" * 64, start: int = 0, end: int = 60 * 60 * 1_000_000_000) -> EpochDefinition:
    return EpochDefinition(
        epoch_id=epoch_id,
        epoch_hash="h" * 64,
        chain_version=CHAIN_VERSION,
        canonical_chain_hash=CHAIN_HASH,
        symbol="BTCUSDT",
        anchor_type="exchange_snapshot",
        anchor_provenance="exchange_websocket_original_payload",
        anchor_event_time_ns=start,
        anchor_receive_time_ns=start,
        anchor_u=1,
        anchor_seq=1,
        anchor_segment_chain_index=0,
        anchor_record_ordinal=1,
        safe_start_ns=start,
        safe_end_ns=end,
        terminating_reason="COMPLETE",
        preceding_gap_id="",
        status="COMPLETE",
    )


def _config(tmp_path: Path, **overrides) -> BuildConfig:
    values = {
        "symbol": "BTCUSDT",
        "input_database": "research_full_ob_continuous_v1_3",
        "output_database": "research_full_ob_silver_v1_3",
        "chain_version": CHAIN_VERSION,
        "expected_chain_hash": CHAIN_HASH,
        "expected_bronze_records": 2_638_997,
        "resume": False,
        "start_chain_index": 0,
        "end_chain_index": 163,
        "chunk_market_minutes": 15,
        "warmup_minutes": 0,
        "max_rss_mib": 1536,
        "min_free_disk_gib": 1.0,
        "min_available_memory_mib": 1,
        "progress_every_chunks": 1,
        "report_path": tmp_path / "readiness.json",
        "lock_path": tmp_path / "build.lock",
        "enforce_canonical_lock_path": False,
    }
    values.update(overrides)
    return BuildConfig(**values)


class FakeReadyClient:
    def __init__(self):
        self.chunks: list[tuple] = []
        self.lc_by_chunk: dict[str, int] = {}
        self.st_by_chunk: dict[str, int] = {}
        self.gaps: list[int] = []
        self.epochs: list[EpochDefinition] = []
        self.schema_tables = 6
        self.settings_seen: list[dict] = []
        self.queries: list[str] = []

    def query(self, sql, parameters=None, settings=None):
        text = str(sql)
        self.queries.append(text)
        if settings:
            self.settings_seen.append(dict(settings))
        parameters = parameters or {}
        if "system.tables" in text:
            return SimpleNamespace(result_rows=[(self.schema_tables,)])
        if "silver_build_chunks_v1_3" in text and "chunk_start_ns" in text:
            return SimpleNamespace(result_rows=list(self.chunks))
        if "silver_epoch_gaps_v1_3" in text and "gap_time_ns" in text:
            return SimpleNamespace(result_rows=[(g,) for g in self.gaps])
        if "replay_epochs_v1_3" in text and "safe_start_ns" in text:
            rows = []
            for epoch in self.epochs:
                rows.append(
                    (
                        epoch.epoch_id,
                        epoch.epoch_hash,
                        epoch.chain_version,
                        epoch.canonical_chain_hash,
                        epoch.symbol,
                        epoch.anchor_type,
                        epoch.anchor_provenance,
                        epoch.anchor_event_time_ns,
                        epoch.anchor_receive_time_ns,
                        epoch.anchor_u,
                        epoch.anchor_seq,
                        epoch.anchor_segment_chain_index,
                        epoch.anchor_record_ordinal,
                        epoch.safe_start_ns,
                        epoch.safe_end_ns,
                        epoch.terminating_reason,
                        epoch.preceding_gap_id,
                        epoch.status,
                        int(epoch.reconnect_resync_proven),
                    )
                )
            return SimpleNamespace(result_rows=rows)
        if "ob_level_changes_v1_3" in text and "count()" in text:
            key = str(parameters.get("chunk_key", ""))
            return SimpleNamespace(result_rows=[(self.lc_by_chunk.get(key, 0),)])
        if "ob_metrics_100ms_v1_3" in text and "count()" in text:
            key = str(parameters.get("chunk_key", ""))
            return SimpleNamespace(result_rows=[(self.st_by_chunk.get(key, 0),)])
        raise AssertionError(f"unexpected query: {text}")

    def close(self):
        return None


def _add_complete(
    client: FakeReadyClient,
    *,
    chunk_key: str,
    epoch_id: str,
    start: int,
    end: int,
    lc: int,
    st: int,
    output_hash: str = HASH_A,
    status: str = "COMPLETE",
):
    client.chunks.append(
        (
            chunk_key,
            epoch_id,
            "h" * 64,
            start,
            end,
            status,
            lc,
            st,
            output_hash,
            1,
        )
    )
    if status == "COMPLETE":
        client.lc_by_chunk[chunk_key] = lc
        client.st_by_chunk[chunk_key] = st


def test_complete_chunk_is_ready(tmp_path):
    client = FakeReadyClient()
    epoch = _epoch()
    client.epochs = [epoch]
    chunks = plan_epoch_chunks([epoch], chunk_market_minutes=15, warmup_minutes=0)
    chunk = chunks[0]
    _add_complete(
        client,
        chunk_key=chunk.chunk_key,
        epoch_id=epoch.epoch_id,
        start=chunk.analysis_start_ns,
        end=chunk.analysis_end_ns,
        lc=10,
        st=2,
    )
    report = readiness.build_readiness_report(client, _config(tmp_path))
    assert report.chunks[0].status == "READY"
    assert report.analyzable_windows[0].status == "READY"
    assert report.analysis_watermark_ns == chunk.analysis_end_ns


def test_running_chunk_is_rejected(tmp_path):
    client = FakeReadyClient()
    epoch = _epoch()
    client.epochs = [epoch]
    chunks = plan_epoch_chunks([epoch], chunk_market_minutes=15, warmup_minutes=0)
    chunk = chunks[0]
    _add_complete(
        client,
        chunk_key=chunk.chunk_key,
        epoch_id=epoch.epoch_id,
        start=chunk.analysis_start_ns,
        end=chunk.analysis_end_ns,
        lc=0,
        st=0,
        status="RUNNING",
        output_hash=readiness.ZERO_HASH,
    )
    # even with rows present
    client.lc_by_chunk[chunk.chunk_key] = 99
    client.st_by_chunk[chunk.chunk_key] = 9
    report = readiness.build_readiness_report(client, _config(tmp_path))
    assert report.chunks[0].status == "NOT_READY"
    assert "RUNNING" in report.chunks[0].reason
    with pytest.raises(readiness.AnalysisReadinessError, match="RUNNING|OUTSIDE|NOT_READY|WINDOW"):
        readiness.assert_analysis_window_ready(
            client,
            _config(tmp_path),
            start_ns=chunk.analysis_start_ns,
            end_ns=chunk.analysis_end_ns,
        )


def test_partial_insert_with_rows_still_rejected_when_running(tmp_path):
    client = FakeReadyClient()
    epoch = _epoch()
    client.epochs = [epoch]
    chunks = plan_epoch_chunks([epoch], chunk_market_minutes=15, warmup_minutes=0)
    chunk = chunks[0]
    _add_complete(
        client,
        chunk_key=chunk.chunk_key,
        epoch_id=epoch.epoch_id,
        start=chunk.analysis_start_ns,
        end=chunk.analysis_end_ns,
        lc=5,
        st=1,
        status="RUNNING",
        output_hash=HASH_A,
    )
    client.lc_by_chunk[chunk.chunk_key] = 5
    client.st_by_chunk[chunk.chunk_key] = 1
    assessment = readiness.classify_chunk(client, _config(tmp_path), readiness.ChunkLedgerRow(
        chunk_key=chunk.chunk_key,
        epoch_id=epoch.epoch_id,
        epoch_hash="h" * 64,
        chunk_start_ns=chunk.analysis_start_ns,
        chunk_end_ns=chunk.analysis_end_ns,
        status="RUNNING",
        level_change_count=5,
        state_count=1,
        output_hash=HASH_A,
        version_ms=1,
    ))
    assert assessment.status == "NOT_READY"


def test_count_or_hash_mismatch_rejected(tmp_path):
    client = FakeReadyClient()
    epoch = _epoch()
    client.epochs = [epoch]
    chunks = plan_epoch_chunks([epoch], chunk_market_minutes=15, warmup_minutes=0)
    chunk = chunks[0]
    _add_complete(
        client,
        chunk_key=chunk.chunk_key,
        epoch_id=epoch.epoch_id,
        start=chunk.analysis_start_ns,
        end=chunk.analysis_end_ns,
        lc=10,
        st=2,
        output_hash=readiness.ZERO_HASH,
    )
    bad_hash = readiness.classify_chunk(
        client,
        _config(tmp_path),
        readiness.ChunkLedgerRow(
            chunk_key=chunk.chunk_key,
            epoch_id=epoch.epoch_id,
            epoch_hash="h" * 64,
            chunk_start_ns=chunk.analysis_start_ns,
            chunk_end_ns=chunk.analysis_end_ns,
            status="COMPLETE",
            level_change_count=10,
            state_count=2,
            output_hash=readiness.ZERO_HASH,
            version_ms=1,
        ),
        verify_counts=False,
    )
    assert bad_hash.status == "NOT_READY"
    assert "OUTPUT_HASH_INVALID" in bad_hash.reason

    client.lc_by_chunk[chunk.chunk_key] = 9
    mismatch = readiness.classify_chunk(
        client,
        _config(tmp_path),
        readiness.ChunkLedgerRow(
            chunk_key=chunk.chunk_key,
            epoch_id=epoch.epoch_id,
            epoch_hash="h" * 64,
            chunk_start_ns=chunk.analysis_start_ns,
            chunk_end_ns=chunk.analysis_end_ns,
            status="COMPLETE",
            level_change_count=10,
            state_count=2,
            output_hash=HASH_A,
            version_ms=1,
        ),
    )
    assert mismatch.status == "NOT_READY"
    assert "COUNT_MISMATCH" in mismatch.reason


def test_window_crossing_chunk_or_gap_rejected(tmp_path):
    client = FakeReadyClient()
    epoch = _epoch()
    client.epochs = [epoch]
    chunks = plan_epoch_chunks([epoch], chunk_market_minutes=15, warmup_minutes=0)
    c1, c2 = chunks[0], chunks[1]
    _add_complete(
        client,
        chunk_key=c1.chunk_key,
        epoch_id=epoch.epoch_id,
        start=c1.analysis_start_ns,
        end=c1.analysis_end_ns,
        lc=1,
        st=1,
        output_hash=HASH_A,
    )
    # c2 missing -> blind hole / incomplete cover
    report = readiness.build_readiness_report(client, _config(tmp_path))
    ready = [row for row in report.chunks if row.status == "READY"]
    crossed = readiness.assess_analysis_window(
        start_ns=c1.analysis_start_ns,
        end_ns=c2.analysis_end_ns,
        ready_chunks=ready,
        gap_times=[],
    )
    assert crossed.status == "NOT_READY"

    gap_ns = c1.analysis_start_ns + 60_000_000_000
    gap_hit = readiness.assess_analysis_window(
        start_ns=c1.analysis_start_ns,
        end_ns=c1.analysis_end_ns,
        ready_chunks=ready,
        gap_times=[gap_ns],
    )
    assert gap_hit.status == "NOT_READY"
    assert "GAP" in gap_hit.reason


def test_adjacent_complete_chunks_same_epoch_merge(tmp_path):
    client = FakeReadyClient()
    epoch = _epoch()
    client.epochs = [epoch]
    chunks = plan_epoch_chunks([epoch], chunk_market_minutes=15, warmup_minutes=0)[:2]
    for chunk, h, lc in zip(chunks, (HASH_A, HASH_B), (11, 22)):
        _add_complete(
            client,
            chunk_key=chunk.chunk_key,
            epoch_id=epoch.epoch_id,
            start=chunk.analysis_start_ns,
            end=chunk.analysis_end_ns,
            lc=lc,
            st=3,
            output_hash=h,
        )
    report = readiness.build_readiness_report(client, _config(tmp_path))
    assert len(report.analyzable_windows) == 1
    window = report.analyzable_windows[0]
    assert window.status == "READY"
    assert window.chunk_keys == [chunks[0].chunk_key, chunks[1].chunk_key]
    assert window.level_change_count == 33
    assert report.analysis_watermark_ns == chunks[1].analysis_end_ns
    ok = readiness.assert_analysis_window_ready(
        client,
        _config(tmp_path),
        start_ns=chunks[0].analysis_start_ns,
        end_ns=chunks[1].analysis_end_ns,
    )
    assert ok.status == "READY"


def test_analysis_does_not_touch_builder_lock(tmp_path):
    lock = tmp_path / "build.lock"
    lock.write_text(json.dumps({"status": "RUNNING", "pid": 1}), encoding="utf-8")
    before = lock.read_text(encoding="utf-8")
    mtime = lock.stat().st_mtime_ns
    info = readiness.ensure_builder_lock_untouched(lock)
    assert info["touched"] is False
    assert info["exists"] is True
    assert lock.read_text(encoding="utf-8") == before
    assert lock.stat().st_mtime_ns == mtime

    client = FakeReadyClient()
    epoch = _epoch()
    client.epochs = [epoch]
    chunks = plan_epoch_chunks([epoch], chunk_market_minutes=15, warmup_minutes=0)
    chunk = chunks[0]
    _add_complete(
        client,
        chunk_key=chunk.chunk_key,
        epoch_id=epoch.epoch_id,
        start=chunk.analysis_start_ns,
        end=chunk.analysis_end_ns,
        lc=4,
        st=1,
    )
    readiness.build_readiness_report(
        client, _config(tmp_path, lock_path=lock), lock_path=lock
    )
    assert lock.read_text(encoding="utf-8") == before
    assert any(settings.get("max_threads") == 1 for settings in client.settings_seen)
    assert any(
        settings.get("max_memory_usage") == readiness.ANALYSIS_MAX_MEMORY_USAGE
        for settings in client.settings_seen
    )


def test_gated_select_rejects_dml_and_unbounded_scan(tmp_path):
    client = FakeReadyClient()
    epoch = _epoch()
    client.epochs = [epoch]
    chunks = plan_epoch_chunks([epoch], chunk_market_minutes=15, warmup_minutes=0)
    chunk = chunks[0]
    _add_complete(
        client,
        chunk_key=chunk.chunk_key,
        epoch_id=epoch.epoch_id,
        start=chunk.analysis_start_ns,
        end=chunk.analysis_end_ns,
        lc=1,
        st=1,
    )
    with pytest.raises(readiness.AnalysisReadinessError, match="SELECT_ONLY"):
        readiness.gated_select(
            client,
            _config(tmp_path),
            "ALTER TABLE x DELETE WHERE 1",
            start_ns=chunk.analysis_start_ns,
            end_ns=chunk.analysis_end_ns,
        )
    with pytest.raises(readiness.AnalysisReadinessError, match="UNBOUNDED_SCAN"):
        readiness.gated_select(
            client,
            _config(tmp_path),
            "SELECT count() FROM research_full_ob_silver_v1_3.ob_level_changes_v1_3",
            start_ns=chunk.analysis_start_ns,
            end_ns=chunk.analysis_end_ns,
        )


def test_resource_guard_stops_on_low_ram(monkeypatch):
    guard = readiness.AnalysisResourceGuard(baseline_swap_used_bytes=0)
    monkeypatch.setattr(readiness, "_available_memory_bytes", lambda: 1024)
    monkeypatch.setattr(readiness, "_swap_used_bytes", lambda: 0)
    with pytest.raises(readiness.AnalysisReadinessError, match="RESOURCE_RAM"):
        guard.check()


def test_resource_guard_stops_on_growing_swap(monkeypatch):
    guard = readiness.AnalysisResourceGuard(baseline_swap_used_bytes=100)
    monkeypatch.setattr(
        readiness, "_available_memory_bytes", lambda: readiness.ANALYSIS_MIN_AVAILABLE_RAM_BYTES
    )
    monkeypatch.setattr(readiness, "_swap_used_bytes", lambda: 200)
    with pytest.raises(readiness.AnalysisReadinessError, match="SWAP_GROWING"):
        guard.check()

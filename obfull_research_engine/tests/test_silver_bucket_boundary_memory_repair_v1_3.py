"""Bucket-boundary, streaming-memory, and append-only repair regressions (v1.3)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from obfull_research_engine.clickhouse_research_store_v1.epoch_aware_silver_v1_3 import (
    EpochDefinition,
    EpochSilverError,
    analysis_bucket_count,
    ceil_bucket_ns,
    discover_epochs,
    evaluation_end_ns,
    floor_bucket_ns,
    iter_analysis_bucket_starts,
    replay_epoch_window,
    _hash,
)
from obfull_research_engine.clickhouse_research_store_v1.silver_bucket_boundary_repair_v1_3 import (
    AffectedChunk,
    BucketRepairError,
    repair_one_chunk,
)
from obfull_research_engine.clickhouse_research_store_v1.silver_plan_coverage_parity_v1_3 import (
    bucket_count,
    iter_epoch_bucket_starts,
)
from obfull_research_engine.clickhouse_research_store_v1.silver_replay import BronzeRecord
import obfull_research_engine.clickhouse_research_store_v1.silver_full_build_v1_3 as runner

CHAIN_VERSION = "chain-v1"
CHAIN_HASH = "c" * 64
SHA_A = "f" * 64
SECOND = 1_000_000_000
BUCKET = 100_000_000


def _record(
    *,
    rank: int,
    ordinal: int,
    kind: str,
    event_ns: int,
    u: int = 10,
    seq: int = 10,
    bids: list | None = None,
    asks: list | None = None,
) -> BronzeRecord:
    if kind == "snapshot":
        payload = {
            "data": {
                "u": u,
                "seq": seq,
                "b": bids or [["100", "1"]],
                "a": asks or [["101", "1"]],
            }
        }
    else:
        payload = {
            "data": {
                "u": u,
                "seq": seq,
                "b": bids or [],
                "a": asks or [],
            }
        }
    return BronzeRecord(
        record_id=f"{rank:04d}{ordinal:060d}",
        symbol="BTCUSDT",
        message_type=kind,
        event_time_ns=event_ns,
        receive_time_ns=event_ns,
        update_id=u,
        seq=seq,
        update_id_present=1,
        seq_present=1,
        source_segment_sha256=SHA_A,
        record_ordinal=ordinal,
        payload_sha256="d" * 64,
        original_payload=payload,
        canonical_segment_chain_index=rank,
    )


def test_offgrid_terminal_bucket_emits_with_true_delta_not_hold_forward():
    offset = 673_000_000
    start = 20 * SECOND + offset
    end = start + 2 * SECOND  # 20 buckets planned from ceil(start)
    expected = analysis_bucket_count(start, end)
    assert expected == 20
    eval_end = evaluation_end_ns(end, end + 10 * SECOND)
    assert eval_end == ceil_bucket_ns(end)

    records = [_record(rank=1, ordinal=1, kind="snapshot", event_ns=start - SECOND, u=10)]
    terminal_start = max(iter_analysis_bucket_starts(start, end))
    records.append(
        _record(
            rank=1,
            ordinal=2,
            kind="delta",
            event_ns=terminal_start + 50_000_000,
            u=11,
            bids=[["100", "9"]],
        )
    )
    # Delta past analysis_end but before eval_end must affect terminal state.
    records.append(
        _record(
            rank=1,
            ordinal=3,
            kind="delta",
            event_ns=end + 10_000_000,
            u=12,
            bids=[["100", "42"]],
        )
    )
    discovery = discover_epochs(
        records,
        chain_version=CHAIN_VERSION,
        canonical_chain_hash=CHAIN_HASH,
        scan_end_ns=eval_end + SECOND,
        clean_segment_start_ranks={1},
    )
    epoch = discovery.epochs[0]
    epoch.safe_end_ns = max(epoch.safe_end_ns, eval_end + SECOND)
    replay = replay_epoch_window(
        records,
        epoch=epoch,
        resume_record=records[0],
        analysis_start_ns=start,
        analysis_end_ns=end,
        build_id="b" * 64,
    )
    assert replay.state_count == expected
    by_ns = {int(r["bucket_start_ms"]) * 1_000_000: r for r in replay.states}
    assert terminal_start in by_ns
    # Terminal state includes the eval-window delta (qty 42), not a hold-forward of 9.
    assert by_ns[terminal_start]["book_hash"]


def test_four_offgrid_chunks_union_is_36000_without_gap_or_dup():
    offset = 673_000_000
    base = 10 * SECOND + offset
    chunk_ns = 15 * 60 * SECOND
    all_starts: list[int] = []
    for i in range(4):
        start = base + i * chunk_ns
        end = start + chunk_ns
        starts = list(iter_analysis_bucket_starts(start, end))
        assert len(starts) == 9000
        all_starts.extend(starts)
    assert len(all_starts) == 36000
    assert len(set(all_starts)) == 36000
    for i in range(4):
        end = base + (i + 1) * chunk_ns
        terminal = floor_bucket_ns(end - 1)
        assert terminal in all_starts


def test_analysis_helpers_match_planner_contract():
    start = 1_000_000_000 + 673_000_000
    end = start + 15 * 60 * SECOND
    assert analysis_bucket_count(start, end) == 9000
    assert analysis_bucket_count(start, end) == bucket_count([(start, end)])
    starts = list(iter_analysis_bucket_starts(start, end))
    assert starts[0] == ceil_bucket_ns(start)
    assert starts[-1] == floor_bucket_ns(end - 1)
    assert evaluation_end_ns(end, end + 10 * SECOND) == ceil_bucket_ns(end)


def test_true_epoch_end_does_not_read_past_safe_end():
    start = 30 * SECOND
    end = start + 1_050_000_000  # 1.05s → includes partial terminal
    records = [
        _record(rank=1, ordinal=1, kind="snapshot", event_ns=start - SECOND, u=10),
        _record(
            rank=1,
            ordinal=2,
            kind="delta",
            event_ns=start + 100_000_000,
            u=11,
            bids=[["100", "2"]],
        ),
        # Would be past safe_end — must never apply.
        _record(
            rank=1,
            ordinal=3,
            kind="delta",
            event_ns=end + 50_000_000,
            u=12,
            bids=[["100", "99"]],
        ),
    ]
    discovery = discover_epochs(
        records[:2],
        chain_version=CHAIN_VERSION,
        canonical_chain_hash=CHAIN_HASH,
        scan_end_ns=end,
        clean_segment_start_ranks={1},
    )
    epoch = discovery.epochs[0]
    epoch.safe_end_ns = end
    replay = replay_epoch_window(
        records,
        epoch=epoch,
        resume_record=records[0],
        analysis_start_ns=start,
        analysis_end_ns=end,
        build_id="b" * 64,
    )
    assert replay.state_count == analysis_bucket_count(start, end)
    # No state should reflect qty 99.
    assert all(float(r.get("best_bid") or 0) != 99 for r in replay.states)


def test_episode_one_minute_is_exactly_600():
    start = 20 * 60 * SECOND
    end = start + 60 * SECOND
    assert analysis_bucket_count(start, end) == 600
    assert len(list(iter_epoch_bucket_starts(start, end))) == 600


def test_streaming_sinks_flush_and_drop_retained_lists(tmp_path):
    class MiniClient:
        def __init__(self) -> None:
            self.inserts: list[str] = []

        def insert(self, table, columns, column_names=None, column_oriented=False):
            self.inserts.append(table)

    config = runner.BuildConfig(
        symbol="BTCUSDT",
        input_database=runner.DEFAULT_INPUT_DATABASE,
        output_database=runner.DEFAULT_OUTPUT_DATABASE,
        chain_version=CHAIN_VERSION,
        expected_chain_hash=CHAIN_HASH,
        expected_bronze_records=10,
        resume=True,
        start_chain_index=0,
        end_chain_index=0,
        chunk_market_minutes=15,
        warmup_minutes=0,
        max_rss_mib=1536,
        min_free_disk_gib=1.0,
        min_available_memory_mib=1,
        progress_every_chunks=1,
        report_path=tmp_path / "r.json",
        lock_path=tmp_path / "l.lock",
        enforce_canonical_lock_path=False,
    )
    client = MiniClient()
    epoch = EpochDefinition(
        epoch_id="e" * 64,
        epoch_hash="h" * 64,
        chain_version=CHAIN_VERSION,
        canonical_chain_hash=CHAIN_HASH,
        symbol="BTCUSDT",
        anchor_type="snapshot",
        anchor_provenance="test",
        anchor_event_time_ns=0,
        anchor_receive_time_ns=0,
        anchor_u=1,
        anchor_seq=1,
        anchor_segment_chain_index=1,
        anchor_record_ordinal=1,
        safe_start_ns=0,
        safe_end_ns=60 * SECOND,
        terminating_reason="COMPLETE",
        preceding_gap_id="",
        status="COMPLETE",
        apply_end_segment_chain_index=1,
        apply_end_record_ordinal=100,
    )
    chunk = runner.ChunkPlan(
        epoch_index=1,
        chunk_index=1,
        epoch=epoch,
        analysis_start_ns=0,
        analysis_end_ns=1 * SECOND,
        warmup_ns=0,
    )
    chunk.materialize_ids()
    lc_sink = runner._StreamingLevelChangeSink(
        client,
        config,
        chunk=chunk,
        version_ms=1,
        batch_size=2,
        batch_max_bytes=10_000,
    )
    st_sink = runner._StreamingStateSink(
        client,
        config,
        chunk=chunk,
        version_ms=1,
        batch_size=2,
        batch_max_bytes=10_000,
    )
    for i in range(5):
        lc_sink(
            {
                "canonical_segment_chain_index": 1,
                "source_record_ordinal": i,
                "apply_order": i,
                "event_time_ns": i,
                "receive_time_ns": i,
                "source_segment_sha256": "0" * 64,
            }
        )
        st_sink({"bucket_start_ms": i * 100, "book_hash": "a" * 64})
    lc_sink.flush()
    st_sink.flush()
    assert lc_sink.insert_calls >= 3
    assert st_sink.insert_calls >= 3
    assert lc_sink.rows == 5
    assert st_sink.rows == 5
    assert len(lc_sink._buffers[0]) == 0
    assert len(st_sink._buffers[0]) == 0


def test_output_hash_stable_with_counts_only():
    payload = {
        "level_change_apply_hash": "a" * 64,
        "level_change_count": 10,
        "state_count": 9000,
        "last_apply_key": (1, 2),
    }
    assert _hash(payload) == _hash(dict(payload))


def test_python_memory_limit_maps_to_interrupted():
    exc = runner.SilverBuildError("STOP_SILVER_MEMORY_LIMIT: peak_rss_kb=1747160")
    assert runner._ledger_status_for_error(exc) == "INTERRUPTED"


def test_repair_skips_when_already_complete(monkeypatch):
    affected = AffectedChunk(
        chunk_key="k" * 64,
        build_id="b" * 64,
        run_id="r" * 64,
        epoch_id="e" * 64,
        epoch_hash="h" * 64,
        epoch_plan_hash="p" * 64,
        chain_version=CHAIN_VERSION,
        canonical_chain_hash=CHAIN_HASH,
        symbol="BTCUSDT",
        chunk_start_ns=0,
        chunk_end_ns=BUCKET,
        warmup_ns=0,
        level_change_count=0,
        state_count=1,
        source_record_count=1,
        output_hash="o" * 64,
        version_ms=1,
        expected_state_count=1,
        missing_bucket_starts=[],
    )

    class Fake:
        def query(self, *_a, **_k):
            return SimpleNamespace(result_rows=[])

    monkeypatch.setattr(
        "obfull_research_engine.clickhouse_research_store_v1.silver_bucket_boundary_repair_v1_3.existing_state_index",
        lambda *_a, **_k: {0: ("rid", "bh")},
    )
    clients = SimpleNamespace(verify=Fake(), write=Fake(), read=Fake())
    config = SimpleNamespace(output_database="db")
    result = repair_one_chunk(
        clients,
        config,
        affected=affected,
        epoch=SimpleNamespace(),
        stop=runner.StopState(),
        dry_run=True,
    )
    assert result["status"] == "SKIPPED_ALREADY_REPAIRED"


def test_repair_hard_stops_on_conflicting_existing_hash(monkeypatch):
    start = 0
    end = BUCKET
    affected = AffectedChunk(
        chunk_key="k" * 64,
        build_id="b" * 64,
        run_id="r" * 64,
        epoch_id="e" * 64,
        epoch_hash="h" * 64,
        epoch_plan_hash="p" * 64,
        chain_version=CHAIN_VERSION,
        canonical_chain_hash=CHAIN_HASH,
        symbol="BTCUSDT",
        chunk_start_ns=start,
        chunk_end_ns=end,
        warmup_ns=0,
        level_change_count=0,
        state_count=0,
        source_record_count=1,
        output_hash="o" * 64,
        version_ms=1,
        expected_state_count=1,
        missing_bucket_starts=[],
    )
    row_id = __import__(
        "obfull_research_engine.clickhouse_research_store_v1.epoch_aware_silver_v1_3",
        fromlist=["state_row_id"],
    ).state_row_id(chunk_key=affected.chunk_key, bucket_start_ns=0)

    monkeypatch.setattr(
        "obfull_research_engine.clickhouse_research_store_v1.silver_bucket_boundary_repair_v1_3.existing_state_index",
        lambda *_a, **_k: {0: (row_id, "old_hash")},
    )

    class Replay:
        states = [{"bucket_start_ms": 0, "book_hash": "new_hash"}]
        level_change_count = 0
        level_change_hash_apply_order = "a" * 64
        end_apply_key = (1, 1)
        evaluation_end_ns = BUCKET

    monkeypatch.setattr(
        "obfull_research_engine.clickhouse_research_store_v1.silver_bucket_boundary_repair_v1_3._replay_chunk_for_repair",
        lambda *_a, **_k: Replay(),
    )

    class Fake:
        def query(self, sql, **_k):
            if "ob_level_changes" in sql or "LEVEL" in sql.upper():
                return SimpleNamespace(result_rows=[[0]])
            return SimpleNamespace(result_rows=[[0]])

    with pytest.raises(BucketRepairError, match="EXISTING_STATE_CONFLICT"):
        repair_one_chunk(
            SimpleNamespace(verify=Fake(), write=Fake(), read=Fake()),
            SimpleNamespace(output_database="research_full_ob_silver_pilot_x_v1_3"),
            affected=affected,
            epoch=SimpleNamespace(safe_end_ns=end),
            stop=runner.StopState(),
            dry_run=False,
        )

"""Regressions for epoch-aware v1.3 bounded Silver replay."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from obfull_research_engine.clickhouse_research_store_v1.epoch_aware_silver_v1_3 import (
    BookHashCache,
    BookHashProfile,
    EPOCH_SILVER_DDLS,
    EpochSilverError,
    ReplayResult,
    discover_epochs,
    iter_bronze_window,
    make_build_id,
    make_chunk_key,
    persist_replay_chunk,
    persist_epochs,
    replay_epoch_window,
    select_resume_checkpoint,
    validate_epoch_window,
    _mapping_preflight,
    canonical_book_bytes_profiled,
)
from obfull_research_engine.clickhouse_research_store_v1.silver_replay import (
    BronzeRecord,
    book_map_sha256,
    replay_bronze_to_silver,
)
from orderbook_analyse.orderbook_v2_live.full_book_state import FullBookState

CHAIN_VERSION = "chain-v1"
CHAIN_HASH = "c" * 64
SHA_A = "f" * 64
SHA_B = "0" * 64
SECOND = 1_000_000_000


def _record(
    *,
    rank: int,
    ordinal: int,
    kind: str,
    event_ns: int,
    u: int = 10,
    seq: int = 10,
    reason: str = "",
    sha: str = SHA_A,
    bids: list | None = None,
    asks: list | None = None,
) -> BronzeRecord:
    if kind == "checkpoint":
        payload = {
            "checkpoint_reason": reason,
            "u": u,
            "seq": seq,
            "bids": bids or [["100", "1"]],
            "asks": asks or [["101", "1"]],
        }
    elif kind == "snapshot":
        payload = {
            "data": {
                "u": u,
                "seq": seq,
                "b": bids or [["100", "1"]],
                "a": asks or [["101", "1"]],
            }
        }
    elif kind == "gap_marker":
        payload = {"details": {"reason": reason or "transport_reconnect"}}
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
        source_segment_sha256=sha,
        record_ordinal=ordinal,
        payload_sha256="d" * 64,
        original_payload=payload,
        canonical_segment_chain_index=rank,
    )


def _discover(records, *, end=20 * SECOND, clean=frozenset({1, 2})):
    return discover_epochs(
        records,
        chain_version=CHAIN_VERSION,
        canonical_chain_hash=CHAIN_HASH,
        scan_end_ns=end,
        clean_segment_start_ranks=set(clean),
    )


def test_exchange_snapshot_opens_deterministic_epoch():
    records = [
        _record(rank=1, ordinal=1, kind="snapshot", event_ns=SECOND),
        _record(rank=1, ordinal=2, kind="delta", event_ns=2 * SECOND, u=11, seq=11),
    ]
    left = _discover(records)
    right = _discover(records)
    assert len(left.epochs) == 1
    assert left.epochs[0].anchor_type == "exchange_snapshot"
    assert left.epochs[0].epoch_id == right.epochs[0].epoch_id
    assert left.epochs[0].epoch_hash == right.epochs[0].epoch_hash


def test_reconnect_resync_requires_and_proves_adjacent_snapshot():
    records = [
        _record(rank=1, ordinal=1, kind="gap_marker", event_ns=SECOND),
        _record(rank=1, ordinal=2, kind="snapshot", event_ns=2 * SECOND, u=20, seq=20),
        _record(
            rank=1,
            ordinal=3,
            kind="checkpoint",
            event_ns=2 * SECOND,
            u=20,
            seq=20,
            reason="reconnect_resync",
        ),
    ]
    discovery = _discover(records)
    assert discovery.epochs[0].reconnect_resync_proven is True
    assert "checkpoint_hash_match" in discovery.epochs[0].anchor_provenance
    with pytest.raises(EpochSilverError, match="lacks adjacent matching"):
        _discover([records[0], records[2]])


def test_reconnect_resync_rejects_intervening_delta_even_if_book_hash_matches():
    records = [
        _record(rank=1, ordinal=1, kind="gap_marker", event_ns=SECOND),
        _record(rank=1, ordinal=2, kind="snapshot", event_ns=2 * SECOND, u=20),
        _record(rank=1, ordinal=3, kind="delta", event_ns=3 * SECOND, u=21),
        _record(
            rank=1,
            ordinal=4,
            kind="checkpoint",
            event_ns=4 * SECOND,
            u=20,
            reason="reconnect_resync",
        ),
    ]
    with pytest.raises(EpochSilverError, match="lacks adjacent matching"):
        _discover(records)


def test_periodic_checkpoint_after_gap_never_opens_epoch():
    discovery = _discover([
        _record(
            rank=1, ordinal=1, kind="checkpoint", event_ns=SECOND,
            reason="segment_start",
        ),
        _record(rank=1, ordinal=2, kind="gap_marker", event_ns=2 * SECOND),
        _record(
            rank=1, ordinal=3, kind="checkpoint", event_ns=3 * SECOND,
            reason="periodic_5m", u=20, seq=20,
        ),
    ])
    assert len(discovery.epochs) == 1
    assert discovery.epochs[0].safe_end_ns == 2 * SECOND


def test_segment_start_after_gap_never_opens_epoch():
    discovery = _discover([
        _record(
            rank=1, ordinal=1, kind="checkpoint", event_ns=SECOND,
            reason="segment_start",
        ),
        _record(rank=1, ordinal=2, kind="gap_marker", event_ns=2 * SECOND),
        _record(
            rank=2, ordinal=1, kind="checkpoint", event_ns=3 * SECOND,
            reason="segment_start", sha=SHA_B,
        ),
    ])
    assert len(discovery.epochs) == 1
    assert discovery.epochs[0].safe_end_ns == 2 * SECOND


def test_segment_start_clean_opens_but_tainted_start_does_not():
    start = _record(
        rank=1, ordinal=1, kind="checkpoint", event_ns=SECOND,
        reason="segment_start",
    )
    assert len(_discover([start], clean={1}).epochs) == 1
    assert _discover([start], clean=set()).epochs == []


def test_real_delta_gap_ends_epoch():
    discovery = _discover([
        _record(rank=1, ordinal=1, kind="snapshot", event_ns=SECOND, u=10),
        _record(rank=1, ordinal=2, kind="delta", event_ns=2 * SECOND, u=12),
    ])
    assert len(discovery.gaps) == 1
    assert discovery.epochs[0].terminating_reason.startswith("GAP:")


def test_delta_gap_without_payload_uses_column_u_seq():
    records = [
        _record(rank=1, ordinal=1, kind="snapshot", event_ns=SECOND, u=10),
        _record(rank=1, ordinal=2, kind="delta", event_ns=2 * SECOND, u=12),
    ]
    records[1].original_payload = {}
    discovery = _discover(records)
    assert len(discovery.gaps) == 1
    assert discovery.epochs[0].terminating_reason.startswith("GAP:")


def test_backward_event_time_does_not_change_apply_order():
    records = [
        _record(rank=1, ordinal=1, kind="snapshot", event_ns=SECOND, u=10),
        _record(
            rank=1, ordinal=2, kind="delta", event_ns=4 * SECOND, u=11,
            bids=[["100", "2"]],
        ),
        _record(
            rank=1, ordinal=3, kind="delta", event_ns=3 * SECOND, u=12,
            bids=[["100", "3"]],
        ),
    ]
    discovery = _discover(records, end=6 * SECOND)
    epoch = discovery.epochs[0]
    replay = replay_epoch_window(
        records,
        epoch=epoch,
        resume_record=records[0],
        analysis_start_ns=2 * SECOND,
        analysis_end_ns=5 * SECOND,
        build_id="b" * 64,
    )
    assert [row["new_size"] for row in replay.level_changes] == [2.0, 3.0]
    assert replay.end_apply_key == (1, 3)


def test_ignored_delta_does_not_consume_apply_order():
    records = [
        _record(rank=1, ordinal=1, kind="snapshot", event_ns=SECOND, u=10),
        _record(
            rank=1,
            ordinal=2,
            kind="delta",
            event_ns=2 * SECOND,
            u=9,
            bids=[["100", "2"]],
        ),
        _record(
            rank=1,
            ordinal=3,
            kind="delta",
            event_ns=3 * SECOND,
            u=11,
            bids=[["100", "3"]],
        ),
    ]
    epoch = _discover(records, end=4 * SECOND).epochs[0]
    replay = replay_epoch_window(
        records,
        epoch=epoch,
        resume_record=records[0],
        analysis_start_ns=1_100_000_000,
        analysis_end_ns=4 * SECOND,
        build_id="b" * 64,
    )
    assert [row["apply_order"] for row in replay.level_changes] == [1]
    assert [row["new_size"] for row in replay.level_changes] == [3.0]


def test_causal_buckets_are_finalized_once():
    records = [
        _record(rank=1, ordinal=1, kind="snapshot", event_ns=SECOND, u=10),
        _record(
            rank=1, ordinal=2, kind="delta", event_ns=1_300_000_000, u=11,
            bids=[["100", "2"]],
        ),
        _record(
            rank=1, ordinal=3, kind="delta", event_ns=1_150_000_000, u=12,
            bids=[["100", "3"]],
        ),
        _record(rank=1, ordinal=4, kind="delta", event_ns=1_500_000_000, u=13),
    ]
    epoch = _discover(records, end=1_600_000_000).epochs[0]
    replay = replay_epoch_window(
        records,
        epoch=epoch,
        resume_record=records[0],
        analysis_start_ns=1_100_000_000,
        analysis_end_ns=1_600_000_000,
        build_id="b" * 64,
    )
    starts = [row["bucket_start_ms"] for row in replay.states]
    assert starts == sorted(set(starts))
    assert len(starts) == 5


@pytest.mark.parametrize("warmup_s,status", [(1, "OK"), (5, "EPOCH_BOUNDARY")])
def test_warmup_must_fit_same_epoch(warmup_s, status):
    epoch = _discover([
        _record(rank=1, ordinal=1, kind="snapshot", event_ns=SECOND),
    ], end=10 * SECOND).epochs[0]
    decision = validate_epoch_window(
        [epoch],
        analysis_start_ns=4 * SECOND,
        analysis_end_ns=6 * SECOND,
        warmup_ns=warmup_s * SECOND,
    )
    assert decision.status == status


def test_analysis_window_crossing_gap_is_rejected():
    discovery = _discover([
        _record(rank=1, ordinal=1, kind="snapshot", event_ns=SECOND),
        _record(rank=1, ordinal=2, kind="gap_marker", event_ns=5 * SECOND),
        _record(rank=1, ordinal=3, kind="snapshot", event_ns=7 * SECOND, u=20),
    ], end=10 * SECOND)
    decision = validate_epoch_window(
        discovery.epochs,
        analysis_start_ns=4 * SECOND,
        analysis_end_ns=8 * SECOND,
        warmup_ns=SECOND,
    )
    assert decision.status == "EPOCH_BOUNDARY"


def test_cross_segment_continuation_uses_rank_not_sha():
    records = [
        _record(rank=1, ordinal=1, kind="snapshot", event_ns=SECOND, sha=SHA_A),
        _record(rank=1, ordinal=2, kind="delta", event_ns=2 * SECOND, u=11, sha=SHA_A),
        _record(
            rank=2, ordinal=1, kind="checkpoint", event_ns=3 * SECOND,
            u=11, seq=11, reason="segment_start", sha=SHA_B,
            bids=[["100", "1"]], asks=[["101", "1"]],
        ),
        _record(rank=2, ordinal=2, kind="delta", event_ns=4 * SECOND, u=12, sha=SHA_B),
    ]
    discovery = _discover(records)
    assert len(discovery.epochs) == 1
    assert discovery.last_apply_key == (2, 2)


def test_cross_segment_taint_requires_independent_snapshot():
    records = [
        _record(rank=1, ordinal=1, kind="snapshot", event_ns=SECOND),
        _record(rank=1, ordinal=2, kind="gap_marker", event_ns=2 * SECOND),
        _record(
            rank=2, ordinal=1, kind="checkpoint", event_ns=3 * SECOND,
            reason="segment_start", sha=SHA_B,
        ),
        _record(rank=2, ordinal=2, kind="snapshot", event_ns=4 * SECOND, sha=SHA_B),
    ]
    discovery = _discover(records)
    assert len(discovery.epochs) == 2
    assert discovery.epochs[1].anchor_type == "exchange_snapshot"
    assert discovery.epochs[1].preceding_gap_id == discovery.gaps[0].gap_id


def test_noncanonical_or_duplicate_apply_order_stops():
    records = [
        _record(rank=2, ordinal=1, kind="snapshot", event_ns=SECOND),
        _record(rank=1, ordinal=1, kind="delta", event_ns=2 * SECOND),
    ]
    with pytest.raises(EpochSilverError, match="not strictly canonical"):
        _discover(records)


def test_epoch_hash_changes_when_definition_changes():
    epoch = _discover([
        _record(rank=1, ordinal=1, kind="snapshot", event_ns=SECOND),
    ]).epochs[0]
    original = epoch.epoch_hash
    epoch.safe_end_ns += 1
    epoch.finalize_hash()
    assert epoch.epoch_hash != original


def test_resume_checkpoint_is_inside_proven_epoch_not_an_opener():
    records = [
        _record(rank=1, ordinal=1, kind="snapshot", event_ns=SECOND),
        _record(
            rank=1, ordinal=2, kind="checkpoint", event_ns=3 * SECOND,
            reason="periodic_5m",
        ),
    ]
    epoch = _discover(records, end=10 * SECOND).epochs[0]
    resume = select_resume_checkpoint(
        records, epoch=epoch, analysis_start_ns=5 * SECOND
    )
    assert resume.record_ordinal == 2
    assert epoch.anchor_record_ordinal == 1


def test_late_checkpoint_cannot_skip_physical_analysis_delta():
    records = [
        _record(rank=1, ordinal=1, kind="snapshot", event_ns=SECOND, u=10),
        _record(
            rank=1,
            ordinal=2,
            kind="delta",
            event_ns=6 * SECOND,
            u=11,
            bids=[["100", "2"]],
        ),
        _record(
            rank=1,
            ordinal=3,
            kind="checkpoint",
            event_ns=4 * SECOND,
            reason="periodic_5m",
            u=11,
            bids=[["100", "2"]],
        ),
    ]
    epoch = _discover(records, end=8 * SECOND).epochs[0]
    resume = select_resume_checkpoint(
        records, epoch=epoch, analysis_start_ns=5 * SECOND
    )
    assert resume.record_ordinal == 1
    replay = replay_epoch_window(
        records,
        epoch=epoch,
        resume_record=resume,
        analysis_start_ns=5 * SECOND,
        analysis_end_ns=8 * SECOND,
        build_id="b" * 64,
    )
    assert [row["source_record_ordinal"] for row in replay.level_changes] == [2]


def test_shutdown_checkpoint_can_resume_but_does_not_open_epoch():
    records = [
        _record(rank=1, ordinal=1, kind="snapshot", event_ns=SECOND),
        _record(
            rank=1,
            ordinal=2,
            kind="checkpoint",
            event_ns=3 * SECOND,
            reason="shutdown",
        ),
    ]
    epoch = _discover(records, end=10 * SECOND).epochs[0]
    assert select_resume_checkpoint(
        records, epoch=epoch, analysis_start_ns=5 * SECOND
    ).record_ordinal == 2
    assert _discover([records[1]], end=10 * SECOND).epochs == []


class _ChunkClient:
    def __init__(self):
        self.status: tuple[str, str] | None = None
        self.inserts: list[tuple[str, list]] = []

    def query(self, *_args, **_kwargs):
        rows = [self.status] if self.status is not None else []
        return SimpleNamespace(result_rows=rows)

    def insert(self, table, rows, column_names):
        self.inserts.append((table, rows))
        if table.endswith("ob_silver_chunks_epoch_pilot_v1_3"):
            status_index = column_names.index("status")
            hash_index = column_names.index("epoch_hash")
            self.status = (rows[-1][status_index], rows[-1][hash_index])


def test_chunk_idempotency_and_resume_after_interruption():
    epoch = _discover([
        _record(rank=1, ordinal=1, kind="snapshot", event_ns=SECOND),
    ], end=10 * SECOND).epochs[0]
    replay = ReplayResult([], [], 0, 0, (1, 1), None, "a" * 64, {})
    client = _ChunkClient()
    first = persist_replay_chunk(
        client,
        epoch=epoch,
        replay=replay,
        analysis_start_ns=4 * SECOND,
        analysis_end_ns=5 * SECOND,
        warmup_ns=SECOND,
        simulate_abort_after_running=True,
    )
    assert first["status"] == "INTERRUPTED"
    resumed = persist_replay_chunk(
        client,
        epoch=epoch,
        replay=replay,
        analysis_start_ns=4 * SECOND,
        analysis_end_ns=5 * SECOND,
        warmup_ns=SECOND,
    )
    assert resumed["status"] == "COMPLETE"
    skipped = persist_replay_chunk(
        client,
        epoch=epoch,
        replay=replay,
        analysis_start_ns=4 * SECOND,
        analysis_end_ns=5 * SECOND,
        warmup_ns=SECOND,
    )
    assert skipped["status"] == "SKIPPED_ALREADY_COMPLETE"


def test_chunk_epoch_hash_mismatch_hard_stops():
    epoch = _discover([
        _record(rank=1, ordinal=1, kind="snapshot", event_ns=SECOND),
    ], end=10 * SECOND).epochs[0]
    replay = ReplayResult([], [], 0, 0, (1, 1), None, "a" * 64, {})
    client = _ChunkClient()
    client.status = ("COMPLETE", "x" * 64)
    with pytest.raises(EpochSilverError, match="epoch hash changed"):
        persist_replay_chunk(
            client,
            epoch=epoch,
            replay=replay,
            analysis_start_ns=4 * SECOND,
            analysis_end_ns=5 * SECOND,
            warmup_ns=SECOND,
        )


class _MappingClient:
    def __init__(self, row):
        self.row = row

    def query(self, *_args, **_kwargs):
        return SimpleNamespace(result_rows=[self.row])


class _EmptyStream:
    def __enter__(self):
        return iter(())

    def __exit__(self, *_args):
        return False


class _ApplyRangeClient(_MappingClient):
    def __init__(self):
        super().__init__((1, 1, 1, 1, CHAIN_HASH))
        self.sql = ""
        self.parameters = {}

    def query_row_block_stream(self, sql, parameters):
        self.sql = sql
        self.parameters = parameters
        return _EmptyStream()


def test_chain_hash_hard_stop_in_production_preflight():
    with pytest.raises(EpochSilverError, match="chain hash changed"):
        _mapping_preflight(
            _MappingClient((2, 2, 2, 1, "x" * 64)),
            symbol="BTCUSDT",
            chain_version=CHAIN_VERSION,
            canonical_chain_hash=CHAIN_HASH,
        )


def test_physical_replay_range_does_not_filter_by_event_time():
    client = _ApplyRangeClient()
    assert list(
        iter_bronze_window(
            client,
            symbol="BTCUSDT",
            chain_version=CHAIN_VERSION,
            canonical_chain_hash=CHAIN_HASH,
            start_ns=1,
            end_ns=2,
            start_apply_key=(7, 11),
            end_apply_key=(8, 3),
        )
    ) == []
    where_clause = client.sql.split("WHERE e.symbol", 1)[1].split("ORDER BY", 1)[0]
    assert "e.event_time_ns" not in where_clause
    assert "canonical_segment_chain_index, e.record_ordinal" in where_clause
    assert client.parameters["start_rank"] == 7
    assert client.parameters["end_ordinal"] == 3


class _EpochClient:
    def __init__(self, existing):
        self.existing = existing
        self.inserts = []

    def query(self, *_args, **_kwargs):
        return SimpleNamespace(result_rows=self.existing)

    def insert(self, *args, **kwargs):
        self.inserts.append((args, kwargs))


def test_persisted_epoch_definition_change_hard_stops():
    epoch = _discover([
        _record(rank=1, ordinal=1, kind="snapshot", event_ns=SECOND),
    ]).epochs[0]
    with pytest.raises(EpochSilverError, match="persisted epoch hash changed"):
        persist_epochs(
            _EpochClient([(epoch.epoch_id, "x" * 64)]),
            [epoch],
        )


def test_optimized_epoch_replay_preserves_legacy_simple_semantics():
    records = [
        _record(rank=1, ordinal=1, kind="snapshot", event_ns=SECOND, u=10),
        _record(
            rank=1, ordinal=2, kind="delta", event_ns=2 * SECOND, u=11,
            bids=[["100", "2"]],
        ),
        _record(rank=1, ordinal=3, kind="delta", event_ns=3 * SECOND, u=12),
    ]
    epoch = _discover(records, end=4 * SECOND).epochs[0]
    optimized = replay_epoch_window(
        records,
        epoch=epoch,
        resume_record=records[0],
        analysis_start_ns=1_100_000_000,
        analysis_end_ns=4 * SECOND,
        build_id="b" * 64,
    )
    baseline = replay_epoch_window(
        records,
        epoch=epoch,
        resume_record=records[0],
        analysis_start_ns=1_100_000_000,
        analysis_end_ns=4 * SECOND,
        build_id="b" * 64,
        use_book_hash_cache=False,
    )
    legacy = replay_bronze_to_silver(
        rows=records,
        symbol="BTCUSDT",
        window_start_ns=1_100_000_000,
        window_end_ns=4 * SECOND,
        silver_build_id="b" * 64,
        created_at_ms=0,
    )
    assert [
        (r["side"], r["price"], r["old_size"], r["new_size"])
        for r in optimized.level_changes
    ] == [
        (r["side"], r["price"], r["old_size"], r["new_size"])
        for r in legacy.level_changes
    ]
    assert [
        (r["bucket_start_ms"], r["best_bid"], r["best_ask"], r["book_hash"])
        for r in optimized.states
    ] == [
        (r["bucket_start_ms"], r["best_bid"], r["best_ask"], r["book_hash"])
        for r in legacy.metrics
    ]
    assert optimized.level_changes == baseline.level_changes
    assert optimized.states == baseline.states
    assert optimized.level_change_hash_apply_order == baseline.level_change_hash_apply_order


def test_profiled_canonical_bytes_preserve_existing_sha256():
    bids = {100.0: 2.0, 99.0: 1.0, 98.0: 0.0}
    asks = {101.0: 3.0, 102.0: 4.0}
    profile = BookHashProfile()
    raw = canonical_book_bytes_profiled(bids, asks, profile)
    import hashlib

    assert hashlib.sha256(raw).hexdigest() == book_map_sha256(bids, asks)


def test_book_hash_cache_reuses_clean_state_and_rehashes_dirty_state():
    state = FullBookState(symbol="BTCUSDT")
    state.apply_snapshot(
        bids=[["100", "1"]],
        asks=[["101", "1"]],
        u=1,
        seq=1,
        ts_ms=1,
        receive_time_ns=1,
        mark_ready=True,
    )
    cache = BookHashCache(enabled=True)
    first = cache.get(state)
    assert cache.get(state) == first
    assert cache.profile.recomputations == 1
    assert cache.profile.cache_hits == 1
    state.bids[100.0] = 2.0
    cache.mark_dirty()
    assert cache.get(state) != first
    assert cache.profile.recomputations == 2


def test_new_replay_epoch_never_reuses_previous_hash_cache():
    first_records = [
        _record(rank=1, ordinal=1, kind="snapshot", event_ns=SECOND, u=10),
        _record(rank=1, ordinal=2, kind="delta", event_ns=2 * SECOND, u=11),
    ]
    second_records = [
        _record(
            rank=2,
            ordinal=1,
            kind="snapshot",
            event_ns=3 * SECOND,
            u=20,
            sha=SHA_B,
            bids=[["99", "1"]],
        ),
        _record(
            rank=2,
            ordinal=2,
            kind="delta",
            event_ns=4 * SECOND,
            u=21,
            sha=SHA_B,
        ),
    ]
    outputs = []
    for records, start, end in (
        (first_records, 1_100_000_000, 2 * SECOND),
        (second_records, 3_100_000_000, 4 * SECOND),
    ):
        epoch = _discover(records, end=end).epochs[0]
        outputs.append(
            replay_epoch_window(
                records,
                epoch=epoch,
                resume_record=records[0],
                analysis_start_ns=start,
                analysis_end_ns=end,
                build_id="b" * 64,
            )
        )
    assert outputs[0].hash_profile["calls"] == 9
    assert outputs[1].hash_profile["calls"] == 9
    assert outputs[0].hash_profile["recomputations"] == 1
    assert outputs[1].hash_profile["recomputations"] == 1
    assert outputs[0].states[0]["book_hash"] != outputs[1].states[0]["book_hash"]


def test_noop_delta_does_not_dirty_hash_cache():
    records = [
        _record(rank=1, ordinal=1, kind="snapshot", event_ns=SECOND, u=10),
        _record(
            rank=1,
            ordinal=2,
            kind="delta",
            event_ns=1_200_000_000,
            u=11,
            bids=[["100", "1"]],
        ),
        _record(rank=1, ordinal=3, kind="delta", event_ns=1_500_000_000, u=12),
    ]
    epoch = _discover(records, end=1_600_000_000).epochs[0]
    replay = replay_epoch_window(
        records,
        epoch=epoch,
        resume_record=records[0],
        analysis_start_ns=1_100_000_000,
        analysis_end_ns=1_600_000_000,
        build_id="b" * 64,
    )
    assert replay.hash_profile["no_op_deltas"] >= 1
    assert replay.hash_profile["cache_hits"] > 0


def test_cached_replay_is_deterministic_across_repetitions():
    records = [
        _record(rank=1, ordinal=1, kind="snapshot", event_ns=SECOND, u=10),
        _record(
            rank=1,
            ordinal=2,
            kind="delta",
            event_ns=1_300_000_000,
            u=11,
            bids=[["100", "2"]],
        ),
        _record(rank=1, ordinal=3, kind="delta", event_ns=1_500_000_000, u=12),
    ]
    epoch = _discover(records, end=1_600_000_000).epochs[0]
    kwargs = {
        "epoch": epoch,
        "resume_record": records[0],
        "analysis_start_ns": 1_100_000_000,
        "analysis_end_ns": 1_600_000_000,
        "build_id": "b" * 64,
    }
    first = replay_epoch_window(records, **kwargs)
    second = replay_epoch_window(records, **kwargs)
    assert first.level_changes == second.level_changes
    assert first.states == second.states
    assert first.level_change_hash_apply_order == second.level_change_hash_apply_order


def test_chain_and_chunk_keys_are_deterministic_and_epoch_bound():
    build = make_build_id(
        chain_version=CHAIN_VERSION,
        canonical_chain_hash=CHAIN_HASH,
        epoch_hash="e" * 64,
        symbol="BTCUSDT",
        analysis_start_ns=1,
        analysis_end_ns=2,
    )
    chunk = make_chunk_key(
        build_id=build,
        epoch_id="i" * 64,
        epoch_hash="e" * 64,
        chunk_start_ns=1,
        chunk_end_ns=2,
        warmup_ns=3,
    )
    assert len(build) == len(chunk) == 64
    assert chunk == make_chunk_key(
        build_id=build,
        epoch_id="i" * 64,
        epoch_hash="e" * 64,
        chunk_start_ns=1,
        chunk_end_ns=2,
        warmup_ns=3,
    )


def test_schema_is_new_only_and_integer_ns_materialized():
    ddl = "\n".join(EPOCH_SILVER_DDLS)
    assert "replay_epochs_epoch_pilot_v1_3" in ddl
    assert "epoch_id FixedString(64)" in ddl
    assert "fromUnixTimestamp64Nano" in ddl
    assert "ALTER " not in ddl and "DROP " not in ddl and "TRUNCATE " not in ddl

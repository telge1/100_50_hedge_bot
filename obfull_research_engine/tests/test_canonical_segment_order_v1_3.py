"""Production-path regressions for v1.3 canonical segment ordering."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import zstandard as zstd

from obfull_research_engine.clickhouse_research_store_v1.canonical_segment_order_v1_3 import (
    DATABASE_V13,
    EVENTS_TABLE_V13,
    SCHEMA_DDLS,
    SEGMENTS_TABLE_V13,
    V13PilotError,
    build_chain_contract,
    register_chain,
    split_v13_safe_epochs,
    validate_stable_extension,
)
from obfull_research_engine.clickhouse_research_store_v1.gap_semantics_audit import (
    list_closed_btc_segments,
)
from obfull_research_engine.clickhouse_research_store_v1.silver_builder import (
    SilverBuildError,
    load_bronze_window,
)
from obfull_research_engine.clickhouse_research_store_v1.silver_replay import (
    BronzeRecord,
    SilverReplayError,
    sort_bronze_source_order,
)


def _record(
    *,
    sha: str,
    rank: int | None,
    ordinal: int,
    event_ns: int,
    kind: str = "delta",
    payload: dict | None = None,
    u: int = 1,
    seq: int = 1,
) -> BronzeRecord:
    return BronzeRecord(
        record_id=f"{rank or 0:02d}{ordinal:062d}"[-64:],
        symbol="BTCUSDT",
        message_type=kind,
        event_time_ns=event_ns,
        receive_time_ns=ordinal,
        update_id=u,
        seq=seq,
        update_id_present=1,
        seq_present=1,
        source_segment_sha256=sha,
        record_ordinal=ordinal,
        payload_sha256="c" * 64,
        original_payload=payload or {},
        canonical_segment_chain_index=rank,
    )


def _ch_row(record: BronzeRecord) -> tuple:
    return (
        record.record_id,
        record.symbol,
        record.message_type,
        record.event_time_ns,
        record.receive_time_ns,
        record.update_id,
        record.seq,
        record.update_id_present,
        record.seq_present,
        record.source_segment_sha256,
        record.record_ordinal,
        record.payload_sha256,
        json.dumps(record.original_payload),
        record.canonical_segment_chain_index,
    )


def test_schema_v13_isolated_and_ns_materialized():
    ddl = "\n".join(SCHEMA_DDLS)
    assert DATABASE_V13 in ddl
    assert SEGMENTS_TABLE_V13 in ddl
    assert EVENTS_TABLE_V13 in ddl
    assert "canonical_segment_chain_index UInt64" in ddl
    assert "fromUnixTimestamp64Nano(segment_start_ns, 'UTC')" in ddl
    assert "fromUnixTimestamp64Nano(event_time_ns, 'UTC')" in ddl
    assert "ALTER " not in ddl and "TRUNCATE " not in ddl and "DROP " not in ddl


def test_production_sort_uses_rank_not_sha_event_u_or_seq():
    rows = [
        _record(sha="0" * 64, rank=8, ordinal=2, event_ns=1, u=1, seq=1),
        _record(sha="f" * 64, rank=7, ordinal=2, event_ns=9, u=9, seq=9),
        _record(sha="0" * 64, rank=8, ordinal=1, event_ns=2, u=2, seq=2),
        _record(sha="f" * 64, rank=7, ordinal=1, event_ns=10, u=10, seq=10),
    ]
    ordered = sort_bronze_source_order(rows)
    assert [(r.canonical_segment_chain_index, r.record_ordinal) for r in ordered] == [
        (7, 1), (7, 2), (8, 1), (8, 2)
    ]
    assert len({(r.source_segment_sha256, r.record_ordinal) for r in ordered}) == 4


def test_production_sort_missing_and_duplicate_rank_hard_stop():
    with pytest.raises(SilverReplayError, match="missing canonical segment rank"):
        sort_bronze_source_order([
            _record(sha="a" * 64, rank=None, ordinal=1, event_ns=1),
            _record(sha="b" * 64, rank=2, ordinal=1, event_ns=2),
        ])
    with pytest.raises(SilverReplayError, match="duplicate or conflicting"):
        sort_bronze_source_order([
            _record(sha="a" * 64, rank=1, ordinal=1, event_ns=1),
            _record(sha="b" * 64, rank=1, ordinal=1, event_ns=2),
        ])
    with pytest.raises(SilverReplayError, match="duplicate segment record ordinal"):
        sort_bronze_source_order([
            _record(sha="a" * 64, rank=1, ordinal=1, event_ns=1),
            _record(sha="a" * 64, rank=1, ordinal=1, event_ns=2),
        ])


class _LoadClient:
    def __init__(self, mapping=(2, 2, 2, 1, "h" * 64), rows=()):
        self.mapping = mapping
        self.rows = list(rows)
        self.sql: list[str] = []

    def query(self, sql, parameters=None):
        self.sql.append(sql)
        if "uniqExact" in sql:
            return SimpleNamespace(result_rows=[self.mapping])
        return SimpleNamespace(result_rows=self.rows)


def test_load_bronze_window_joins_metadata_and_orders_by_rank():
    records = [
        _record(sha="f" * 64, rank=2, ordinal=1, event_ns=1),
        _record(sha="0" * 64, rank=3, ordinal=1, event_ns=2),
    ]
    client = _LoadClient(rows=[_ch_row(row) for row in records])
    rows, _ = load_bronze_window(
        client,
        symbol="BTCUSDT",
        window_start="2026-09-06T07:00:00Z",
        window_end="2026-09-06T08:00:00Z",
        chain_version="chain",
        canonical_chain_hash="h" * 64,
    )
    assert len(rows) == 2
    sql = client.sql[-1]
    assert f"{DATABASE_V13}.canonical_segments_pilot_v1_3" in sql
    assert "ORDER BY s.canonical_segment_chain_index, e.record_ordinal" in sql
    assert "ORDER BY source_segment_sha256" not in sql


@pytest.mark.parametrize(
    "mapping",
    [
        (0, 0, 0, 0, ""),
        (2, 2, 1, 1, "h" * 64),
        (2, 1, 2, 1, "h" * 64),
    ],
)
def test_load_bronze_window_rejects_missing_or_duplicate_mapping(mapping):
    with pytest.raises(SilverBuildError, match="SEGMENT_MAPPING_INVALID"):
        load_bronze_window(
            _LoadClient(mapping=mapping),
            symbol="BTCUSDT",
            window_start="2026-09-06T07:00:00Z",
            window_end="2026-09-06T08:00:00Z",
            chain_version="chain",
            canonical_chain_hash="h" * 64,
        )


def test_load_bronze_window_requires_and_validates_chain_hash():
    with pytest.raises(SilverBuildError, match="expected canonical chain hash"):
        load_bronze_window(
            _LoadClient(),
            symbol="BTCUSDT",
            window_start="2026-09-06T07:00:00Z",
            window_end="2026-09-06T08:00:00Z",
            chain_version="chain",
        )
    with pytest.raises(SilverBuildError, match="canonical chain hash changed"):
        load_bronze_window(
            _LoadClient(mapping=(2, 2, 2, 1, "x" * 64)),
            symbol="BTCUSDT",
            window_start="2026-09-06T07:00:00Z",
            window_end="2026-09-06T08:00:00Z",
            chain_version="chain",
            canonical_chain_hash="h" * 64,
        )


def _write_segment(root: Path, name: str, archive_ns: int, sha: str, hour: str) -> Path:
    directory = root / "BTCUSDT" / "2026" / "09" / "06"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    record = {
        "message_type": "delta",
        "event_time_ns": archive_ns,
        "receive_time_ns": archive_ns,
        "archive_time_ns": archive_ns,
        "u": 1,
        "seq": 1,
        "payload_sha256": "d" * 64,
        "original_payload": {"data": {"u": 1, "seq": 1}},
    }
    path.write_bytes(zstd.ZstdCompressor().compress((json.dumps(record) + "\n").encode()))
    Path(str(path) + ".manifest.json").write_text(json.dumps({
        "utc_hour": hour,
        "segment_start": hour,
        "segment_sha256": sha,
    }))
    return path


def test_list_closed_segments_ignores_lexical_path_order(tmp_path: Path):
    late = _write_segment(
        tmp_path,
        "BTCUSDT_20260906T070000Z_0000_full_ob_continuous_raw_archive_v1.ndjson.zst",
        200,
        "0" * 64,
        "2026-09-06T07:00:00Z",
    )
    early = _write_segment(
        tmp_path,
        "BTCUSDT_20260906T070000Z_ffff_full_ob_continuous_raw_archive_v1.ndjson.zst",
        100,
        "f" * 64,
        "2026-09-06T07:00:00Z",
    )
    _write_segment(
        tmp_path,
        "BTCUSDT_20260906T080000Z_live_full_ob_continuous_raw_archive_v1.ndjson.zst",
        300,
        "a" * 64,
        "2026-09-06T08:00:00Z",
    )
    ordered = list_closed_btc_segments(tmp_path)
    assert [path for path, _ in ordered] == [early, late]


def test_stable_indices_append_only_and_retroactive_change_stops():
    old = [
        {
            "canonical_segment_chain_index": 0,
            "source_segment_sha256": "a",
            "segment_start_ns": 0,
            "first_archive_time_ns": 1,
            "first_receive_time_ns": 1,
        }
    ]
    appended = old + [{
        "canonical_segment_chain_index": 1,
        "source_segment_sha256": "b",
        "segment_start_ns": 2,
        "first_archive_time_ns": 3,
        "first_receive_time_ns": 3,
    }]
    validate_stable_extension(old, appended)
    changed = [{**old[0], "source_segment_sha256": "z"}]
    with pytest.raises(V13PilotError, match="retroactive change"):
        validate_stable_extension(old, changed)


def test_cross_segment_epoch_gap_periodic_then_snapshot():
    rows = [
        _record(
            sha="f" * 64, rank=10, ordinal=1, event_ns=100,
            kind="checkpoint", payload={"checkpoint_reason": "segment_start"},
        ),
        _record(sha="f" * 64, rank=10, ordinal=2, event_ns=90, u=5, seq=5),
        _record(
            sha="0" * 64, rank=11, ordinal=1, event_ns=80,
            kind="checkpoint", payload={"checkpoint_reason": "segment_start"},
        ),
        _record(sha="0" * 64, rank=11, ordinal=2, event_ns=70, kind="gap_marker"),
        _record(
            sha="0" * 64, rank=11, ordinal=3, event_ns=60,
            kind="checkpoint", payload={"checkpoint_reason": "periodic_5m"},
        ),
        _record(sha="0" * 64, rank=11, ordinal=4, event_ns=50, kind="snapshot"),
        _record(sha="0" * 64, rank=11, ordinal=5, event_ns=40),
    ]
    epochs = split_v13_safe_epochs(rows)
    assert [epoch["anchor_type"] for epoch in epochs] == [
        "segment_start_clean", "exchange_snapshot"
    ]
    assert epochs[0]["terminating_reason"] == "gap_marker"
    assert epochs[0]["end_apply_key"] == [11, 2]
    assert epochs[1]["start_apply_key"] == [11, 4]


class _RegistryClient:
    def __init__(self, existing=()):
        self.existing = list(existing)
        self.inserts = []

    def query(self, *_args, **_kwargs):
        return SimpleNamespace(result_rows=self.existing)

    def insert(self, table, rows, column_names):
        self.inserts.append((table, rows, column_names))


def test_chain_registration_is_idempotent_and_hash_change_stops():
    segment = {
        "symbol": "BTCUSDT",
        "canonical_segment_chain_index": 0,
        "source_segment_sha256": "a" * 64,
        "source_path": "/x",
        "segment_start_ns": 1,
        "segment_end_ns": 2,
        "first_archive_time_ns": 1,
        "last_archive_time_ns": 2,
        "first_event_time_ns": 1,
        "last_event_time_ns": 2,
        "first_receive_time_ns": 1,
        "last_receive_time_ns": 2,
        "archive_instance_id": "i",
        "record_count": 1,
        "resolution_status": "UNIQUE_HOUR_CANONICAL",
        "is_canonical": 1,
        "predecessor_segment_sha256": "",
        "anchor_type": "segment_start",
        "anchor_provenance": "clean",
        "continuity_status": "COMPLETE",
    }
    contract = {
        "chain_version": "v",
        "canonical_chain_hash": "b" * 64,
        "segments": [segment],
    }
    first = _RegistryClient()
    assert register_chain(first, contract)["status"] == "INSERTED"
    assert len(first.inserts) == 1
    second = _RegistryClient(existing=[(0, "a" * 64, "b" * 64)])
    assert register_chain(second, contract)["status"] == "SKIPPED_IDENTICAL_CHAIN"
    changed = _RegistryClient(existing=[(0, "a" * 64, "c" * 64)])
    with pytest.raises(V13PilotError, match="registered chain differs"):
        register_chain(changed, contract)


def test_episode1_committed_parity_reference_unchanged():
    report = json.loads((
        Path(__file__).parents[1]
        / "runs/clickhouse_research_store_pilot_v1/detail_parity_report.json"
    ).read_text())
    assert report["level_change_parity"]["reference_rows"] == 30_939
    assert report["level_change_parity"]["clickhouse_rows"] == 30_939
    assert report["level_change_parity"]["reference_hash"] == report["level_change_parity"]["clickhouse_hash"]
    assert report["regressions"]["states_100ms"]["compared_buckets"] == 600
    assert report["regressions"]["states_100ms"]["status"] == "PARITY_EXACT"

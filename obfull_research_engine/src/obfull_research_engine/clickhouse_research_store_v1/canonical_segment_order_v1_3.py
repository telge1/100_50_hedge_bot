"""Versioned v1.3 canonical segment registry and bounded Bronze pilots.

The v1/v1.2 tables are never modified. Segment SHA-256 is identity only;
cross-segment application order is the persisted canonical chain index.
"""

from __future__ import annotations

import hashlib
import json
import resource
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


def _ensure_orderbook_analyse_src() -> None:
    sibling = Path(__file__).resolve().parents[4].parent / "orderbook_analyse" / "src"
    if sibling.is_dir() and str(sibling) not in sys.path:
        sys.path.insert(0, str(sibling))


_ensure_orderbook_analyse_src()

from .detail_parity import canonical_lc_row, hash_canonical_lcs
from .gap_semantics_audit import iter_records_stream
from .helpers import get_clickhouse_client, iso_to_ns_exact, make_record_id
from .silver_builder import load_bronze_window
from .silver_replay import (
    BronzeRecord,
    bronze_row_from_ch,
    replay_bronze_to_silver,
    sort_bronze_source_order,
)

DATABASE_V13 = "research_full_ob_continuous_pilot_v1_3"
SEGMENTS_TABLE_V13 = "canonical_segments_pilot_v1_3"
EVENTS_TABLE_V13 = "raw_full_ob_events_pilot_v1_3"
LEDGER_TABLE_V13 = "raw_full_ob_import_segments_pilot_v1_3"
CHECKPOINTS_TABLE_V13 = "ob_checkpoints_pilot_v1_3"
LEVEL_CHANGES_TABLE_V13 = "ob_level_changes_pilot_v1_3"
METRICS_TABLE_V13 = "ob_metrics_100ms_pilot_v1_3"
BUILDS_TABLE_V13 = "ob_silver_builds_pilot_v1_3"
CHAIN_SCHEMA_VERSION = "canonical_segment_chain_v1_3"
EVENT_SCHEMA_VERSION = "raw_full_ob_continuous_pilot_v1_3"
RSS_LIMIT_KB = 1_500 * 1024
BATCH_SIZE = 500


class V13PilotError(RuntimeError):
    """Hard-stop error carrying a STOP_V1_3_* verdict."""


SCHEMA_DDLS = (
    f"CREATE DATABASE IF NOT EXISTS {DATABASE_V13}",
    f"""
    CREATE TABLE IF NOT EXISTS {DATABASE_V13}.{SEGMENTS_TABLE_V13}
    (
        symbol LowCardinality(String),
        chain_version String,
        canonical_segment_chain_index UInt64,
        source_segment_sha256 FixedString(64),
        source_path String,
        segment_start_ns UInt64,
        segment_start DateTime64(9, 'UTC')
            MATERIALIZED fromUnixTimestamp64Nano(segment_start_ns, 'UTC'),
        segment_end_ns UInt64,
        segment_end DateTime64(9, 'UTC')
            MATERIALIZED fromUnixTimestamp64Nano(segment_end_ns, 'UTC'),
        first_archive_time_ns UInt64,
        last_archive_time_ns UInt64,
        first_event_time_ns UInt64,
        last_event_time_ns UInt64,
        first_receive_time_ns UInt64,
        last_receive_time_ns UInt64,
        archive_instance_id String,
        record_count UInt64,
        resolution_status LowCardinality(String),
        is_canonical UInt8,
        predecessor_segment_sha256 String,
        anchor_type LowCardinality(String),
        anchor_provenance String,
        continuity_status LowCardinality(String),
        canonical_chain_hash FixedString(64),
        created_at_ms UInt64,
        created_at DateTime64(3, 'UTC')
            MATERIALIZED fromUnixTimestamp64Milli(created_at_ms, 'UTC')
    )
    ENGINE = ReplacingMergeTree(created_at_ms)
    ORDER BY (symbol, chain_version, source_segment_sha256)
    """.strip(),
    f"""
    CREATE TABLE IF NOT EXISTS {DATABASE_V13}.{EVENTS_TABLE_V13}
    (
        record_id FixedString(64),
        symbol LowCardinality(String),
        chain_version String,
        canonical_segment_chain_index UInt64,
        event_time_ns UInt64,
        event_time DateTime64(9, 'UTC')
            MATERIALIZED fromUnixTimestamp64Nano(event_time_ns, 'UTC'),
        receive_time_ns UInt64,
        receive_time DateTime64(9, 'UTC')
            MATERIALIZED fromUnixTimestamp64Nano(receive_time_ns, 'UTC'),
        message_type LowCardinality(String),
        update_id UInt64,
        seq UInt64,
        update_id_present UInt8,
        seq_present UInt8,
        event_time_present UInt8,
        receive_time_present UInt8,
        source_path String,
        source_segment_sha256 FixedString(64),
        record_ordinal UInt64,
        payload_sha256 FixedString(64),
        original_payload String,
        envelope_version String,
        ingestion_ts_ms UInt64,
        ingestion_ts DateTime64(3, 'UTC')
            MATERIALIZED fromUnixTimestamp64Milli(ingestion_ts_ms, 'UTC')
    )
    ENGINE = ReplacingMergeTree(ingestion_ts_ms)
    PARTITION BY (symbol, toYYYYMMDD(event_time))
    ORDER BY (symbol, chain_version, canonical_segment_chain_index, record_ordinal, record_id)
    """.strip(),
    f"""
    CREATE TABLE IF NOT EXISTS {DATABASE_V13}.{LEDGER_TABLE_V13}
    (
        import_id FixedString(64),
        chain_version String,
        canonical_chain_hash FixedString(64),
        symbol LowCardinality(String),
        window_name LowCardinality(String),
        window_start_ns UInt64,
        window_end_ns UInt64,
        status LowCardinality(String),
        rows_seen UInt64,
        rows_inserted UInt64,
        error_message String,
        ledger_version UInt64,
        created_at DateTime64(3, 'UTC')
            MATERIALIZED fromUnixTimestamp64Milli(ledger_version, 'UTC')
    )
    ENGINE = ReplacingMergeTree(ledger_version)
    ORDER BY import_id
    """.strip(),
    f"""
    CREATE TABLE IF NOT EXISTS {DATABASE_V13}.{CHECKPOINTS_TABLE_V13}
    (
        silver_build_id FixedString(64),
        chain_version String,
        canonical_segment_chain_index UInt64,
        source_record_id FixedString(64),
        source_record_ordinal UInt64,
        checkpoint_time_ns UInt64,
        checkpoint_time DateTime64(9, 'UTC')
            MATERIALIZED fromUnixTimestamp64Nano(checkpoint_time_ns, 'UTC'),
        payload String,
        created_at_ms UInt64
    )
    ENGINE = ReplacingMergeTree(created_at_ms)
    ORDER BY (silver_build_id, canonical_segment_chain_index, source_record_ordinal)
    """.strip(),
    f"""
    CREATE TABLE IF NOT EXISTS {DATABASE_V13}.{LEVEL_CHANGES_TABLE_V13}
    (
        silver_build_id FixedString(64),
        chain_version String,
        canonical_segment_chain_index UInt64,
        source_record_id FixedString(64),
        source_record_ordinal UInt64,
        event_time_ns UInt64,
        event_time DateTime64(9, 'UTC')
            MATERIALIZED fromUnixTimestamp64Nano(event_time_ns, 'UTC'),
        payload String,
        created_at_ms UInt64
    )
    ENGINE = ReplacingMergeTree(created_at_ms)
    ORDER BY (silver_build_id, canonical_segment_chain_index, source_record_ordinal)
    """.strip(),
    f"""
    CREATE TABLE IF NOT EXISTS {DATABASE_V13}.{METRICS_TABLE_V13}
    (
        silver_build_id FixedString(64),
        chain_version String,
        replay_epoch UInt64,
        bucket_start_ms UInt64,
        bucket_start DateTime64(3, 'UTC')
            MATERIALIZED fromUnixTimestamp64Milli(bucket_start_ms, 'UTC'),
        payload String,
        created_at_ms UInt64
    )
    ENGINE = ReplacingMergeTree(created_at_ms)
    ORDER BY (silver_build_id, replay_epoch, bucket_start_ms)
    """.strip(),
    f"""
    CREATE TABLE IF NOT EXISTS {DATABASE_V13}.{BUILDS_TABLE_V13}
    (
        silver_build_id FixedString(64),
        chain_version String,
        canonical_chain_hash FixedString(64),
        status LowCardinality(String),
        created_at_ms UInt64,
        created_at DateTime64(3, 'UTC')
            MATERIALIZED fromUnixTimestamp64Milli(created_at_ms, 'UTC')
    )
    ENGINE = ReplacingMergeTree(created_at_ms)
    ORDER BY silver_build_id
    """.strip(),
)


SEGMENT_COLUMNS = [
    "symbol",
    "chain_version",
    "canonical_segment_chain_index",
    "source_segment_sha256",
    "source_path",
    "segment_start_ns",
    "segment_end_ns",
    "first_archive_time_ns",
    "last_archive_time_ns",
    "first_event_time_ns",
    "last_event_time_ns",
    "first_receive_time_ns",
    "last_receive_time_ns",
    "archive_instance_id",
    "record_count",
    "resolution_status",
    "is_canonical",
    "predecessor_segment_sha256",
    "anchor_type",
    "anchor_provenance",
    "continuity_status",
    "canonical_chain_hash",
    "created_at_ms",
]

EVENT_COLUMNS = [
    "record_id",
    "symbol",
    "chain_version",
    "canonical_segment_chain_index",
    "event_time_ns",
    "receive_time_ns",
    "message_type",
    "update_id",
    "seq",
    "update_id_present",
    "seq_present",
    "event_time_present",
    "receive_time_present",
    "source_path",
    "source_segment_sha256",
    "record_ordinal",
    "payload_sha256",
    "original_payload",
    "envelope_version",
    "ingestion_ts_ms",
]


def _rss_kb() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def _check_rss() -> None:
    if _rss_kb() > RSS_LIMIT_KB:
        raise V13PilotError(f"STOP_RESOURCE_LIMIT: peak_rss_kb={_rss_kb()}")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _chain_temporal_key(row: dict[str, Any]) -> tuple[int, int, int, str]:
    return (
        int(row["segment_start_ns"]),
        int(row["first_archive_time_ns"]),
        int(row["first_receive_time_ns"]),
        str(row["source_segment_sha256"]),
    )


def _anchor(summary: dict[str, Any]) -> tuple[str, str]:
    counts = summary.get("anchor_counts") or {}
    if int(counts.get("exchange_snapshot") or 0) > 0:
        return "exchange_snapshot", "exchange_websocket_original_payload"
    if int(counts.get("reconnect_resync") or 0) > 0:
        return "reconnect_resync", "in_memory_after_exchange_snapshot_applied"
    return "segment_start", "in_memory_snapshot_continuation"


def build_chain_contract(contract_path: Path) -> dict[str, Any]:
    """Build an immutable chain version from the proven segment-order artifact."""
    source = json.loads(contract_path.read_text(encoding="utf-8"))
    if source.get("verdict") != "BTC_FULL_OB_SEGMENT_ORDER_PROVEN":
        raise V13PilotError("STOP_V1_3_SEGMENT_CHAIN_AMBIGUOUS: source contract not proven")
    repeated = {
        row["utc_hour"]: row["resolution"]
        for row in source.get("repeated_utc_hours", [])
    }
    if any(str(value).startswith("STOP_") for value in repeated.values()):
        raise V13PilotError("STOP_V1_3_SEGMENT_CHAIN_AMBIGUOUS: unresolved repeated hour")

    segments: list[dict[str, Any]] = []
    predecessor = ""
    for expected_index, summary in enumerate(source.get("canonical_segments", [])):
        if int(summary.get("segment_chain_index", -1)) != expected_index:
            raise V13PilotError("STOP_V1_3_CHAIN_NOT_STABLE: non-contiguous source indices")
        anchor_type, provenance = _anchor(summary)
        row = {
            "symbol": "BTCUSDT",
            "canonical_segment_chain_index": expected_index,
            "source_segment_sha256": str(summary["segment_sha256"]),
            "source_path": str(summary["path"]),
            "segment_start_ns": int(summary["segment_start_ns"]),
            "segment_end_ns": int(summary["segment_end_ns"]),
            "first_archive_time_ns": int(summary["first_archive_time_ns"]),
            "last_archive_time_ns": int(summary["last_archive_time_ns"]),
            "first_event_time_ns": int(summary["first_record_event_ns"]),
            "last_event_time_ns": int(summary["last_record_event_ns"]),
            "first_receive_time_ns": int(summary["first_receive_time_ns"]),
            "last_receive_time_ns": int(summary["last_receive_time_ns"]),
            "archive_instance_id": str(summary.get("archive_instance_id") or ""),
            "record_count": int(summary["record_count_scanned"]),
            "resolution_status": repeated.get(
                str(summary.get("utc_hour")), "UNIQUE_HOUR_CANONICAL"
            ),
            "is_canonical": 1,
            "predecessor_segment_sha256": predecessor,
            "anchor_type": anchor_type,
            "anchor_provenance": provenance,
            "continuity_status": str(summary.get("completion_status") or ""),
        }
        segments.append(row)
        predecessor = row["source_segment_sha256"]

    if not segments or segments != sorted(segments, key=_chain_temporal_key):
        raise V13PilotError("STOP_V1_3_SEGMENT_CHAIN_AMBIGUOUS: temporal order mismatch")
    hash_payload = [
        {key: value for key, value in row.items() if key != "source_path"}
        for row in segments
    ]
    chain_hash = hashlib.sha256(_canonical_json(hash_payload)).hexdigest()
    end_ns = max(row["last_archive_time_ns"] for row in segments)
    end_text = datetime.fromtimestamp(end_ns // 1_000_000_000, tz=timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    chain_version = f"{CHAIN_SCHEMA_VERSION}_BTCUSDT_{end_text}_{chain_hash[:16]}"
    return {
        "chain_version": chain_version,
        "canonical_chain_hash": chain_hash,
        "segments": segments,
        "source_contract_sha256": hashlib.sha256(contract_path.read_bytes()).hexdigest(),
    }


def validate_stable_extension(
    old_segments: list[dict[str, Any]], new_segments: list[dict[str, Any]]
) -> None:
    """Require an identical prefix; retroactive change needs a new chain version."""
    if len(new_segments) < len(old_segments):
        raise V13PilotError("STOP_V1_3_CHAIN_NOT_STABLE: chain shrank")
    stable_fields = (
        "canonical_segment_chain_index",
        "source_segment_sha256",
        "segment_start_ns",
        "first_archive_time_ns",
        "first_receive_time_ns",
    )
    for old, new in zip(old_segments, new_segments):
        if any(old.get(field) != new.get(field) for field in stable_fields):
            raise V13PilotError(
                "STOP_V1_3_CHAIN_NOT_STABLE: retroactive change requires new chain_version"
            )
    if len(new_segments) > len(old_segments):
        previous = old_segments[-1] if old_segments else None
        first_new = new_segments[len(old_segments)]
        if previous and _chain_temporal_key(first_new) <= _chain_temporal_key(previous):
            raise V13PilotError(
                "STOP_V1_3_CHAIN_NOT_STABLE: appended segment is not strictly later"
            )


def split_v13_safe_epochs(rows: list[BronzeRecord]) -> list[dict[str, Any]]:
    """Split canonically ordered Bronze records at true gaps and independent anchors."""
    ordered = sort_bronze_source_order(rows)
    epochs: list[dict[str, Any]] = []
    active: dict[str, Any] | None = None
    tainted = False
    for row in ordered:
        payload = row.original_payload if isinstance(row.original_payload, dict) else {}
        kind = row.message_type
        reason = str(payload.get("checkpoint_reason") or "")
        if kind == "gap_marker":
            if active is not None:
                active["end_apply_key"] = [
                    int(row.canonical_segment_chain_index),
                    row.record_ordinal,
                ]
                active["terminating_reason"] = "gap_marker"
                epochs.append(active)
                active = None
            tainted = True
            continue
        if kind == "snapshot":
            active = {
                "epoch_index": len(epochs) + 1,
                "anchor_type": "exchange_snapshot",
                "start_apply_key": [
                    int(row.canonical_segment_chain_index),
                    row.record_ordinal,
                ],
                "end_apply_key": None,
                "terminating_reason": "open",
            }
            tainted = False
            continue
        if active is None and not tainted and kind == "checkpoint" and reason == "segment_start":
            active = {
                "epoch_index": len(epochs) + 1,
                "anchor_type": "segment_start_clean",
                "start_apply_key": [
                    int(row.canonical_segment_chain_index),
                    row.record_ordinal,
                ],
                "end_apply_key": None,
                "terminating_reason": "open",
            }
        # periodic_5m and segment_start while tainted deliberately do nothing.
    if active is not None:
        active["terminating_reason"] = "input_end"
        epochs.append(active)
    return epochs


def ensure_v13_schema(client: Any) -> None:
    client.command("SET max_threads = 1")
    for ddl in SCHEMA_DDLS:
        client.command(ddl)


def _segment_db_tuple(
    row: dict[str, Any], chain_version: str, chain_hash: str, created_at_ms: int
) -> tuple[Any, ...]:
    return tuple(
        {
            **row,
            "chain_version": chain_version,
            "canonical_chain_hash": chain_hash,
            "created_at_ms": created_at_ms,
        }[column]
        for column in SEGMENT_COLUMNS
    )


def register_chain(client: Any, contract: dict[str, Any]) -> dict[str, Any]:
    chain_version = str(contract["chain_version"])
    expected = contract["segments"]
    existing = client.query(
        f"""
        SELECT
          canonical_segment_chain_index, source_segment_sha256, canonical_chain_hash
        FROM {DATABASE_V13}.{SEGMENTS_TABLE_V13} FINAL
        WHERE symbol = 'BTCUSDT' AND chain_version = {{chain_version:String}}
        ORDER BY canonical_segment_chain_index
        """,
        parameters={"chain_version": chain_version},
    ).result_rows
    if existing:
        if len(existing) != len(expected):
            raise V13PilotError("STOP_V1_3_CHAIN_NOT_STABLE: registered chain length changed")
        for expected_row, actual in zip(expected, existing):
            rank, sha, chain_hash = actual
            sha = sha.decode() if isinstance(sha, bytes) else str(sha)
            chain_hash = chain_hash.decode() if isinstance(chain_hash, bytes) else str(chain_hash)
            if (
                int(rank) != int(expected_row["canonical_segment_chain_index"])
                or sha != expected_row["source_segment_sha256"]
                or chain_hash != contract["canonical_chain_hash"]
            ):
                raise V13PilotError("STOP_V1_3_CHAIN_NOT_STABLE: registered chain differs")
        return {"status": "SKIPPED_IDENTICAL_CHAIN", "rows": len(existing)}

    created_at_ms = int(time.time() * 1000)
    client.insert(
        f"{DATABASE_V13}.{SEGMENTS_TABLE_V13}",
        [
            _segment_db_tuple(row, chain_version, contract["canonical_chain_hash"], created_at_ms)
            for row in expected
        ],
        column_names=SEGMENT_COLUMNS,
    )
    return {"status": "INSERTED", "rows": len(expected)}


def _event_row(
    record: dict[str, Any],
    ordinal: int,
    segment: dict[str, Any],
    chain_version: str,
    ingestion_ts_ms: int,
) -> tuple[Any, ...]:
    payload = record.get("original_payload")
    payload_text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    u = record.get("u")
    seq = record.get("seq")
    event_ns = record.get("event_time_ns")
    receive_ns = record.get("receive_time_ns")
    values = {
        "record_id": make_record_id(
            source_segment_sha256=segment["source_segment_sha256"],
            record_ordinal=ordinal,
        ),
        "symbol": "BTCUSDT",
        "chain_version": chain_version,
        "canonical_segment_chain_index": segment["canonical_segment_chain_index"],
        "event_time_ns": int(event_ns or 0),
        "receive_time_ns": int(receive_ns or 0),
        "message_type": str(record.get("message_type") or ""),
        "update_id": int(u or 0),
        "seq": int(seq or 0),
        "update_id_present": int(u is not None),
        "seq_present": int(seq is not None),
        "event_time_present": int(event_ns is not None),
        "receive_time_present": int(receive_ns is not None),
        "source_path": segment["source_path"],
        "source_segment_sha256": segment["source_segment_sha256"],
        "record_ordinal": ordinal,
        "payload_sha256": str(record.get("payload_sha256") or ""),
        "original_payload": payload_text,
        "envelope_version": str(record.get("schema_version") or "1"),
        "ingestion_ts_ms": ingestion_ts_ms,
    }
    if len(values["payload_sha256"]) != 64:
        raise V13PilotError("STOP_V1_3_BRONZE_SEGMENT_MAPPING_INVALID: payload SHA missing")
    return tuple(values[column] for column in EVENT_COLUMNS)


def _ledger_status(client: Any, import_id: str) -> str | None:
    rows = client.query(
        f"""
        SELECT status
        FROM {DATABASE_V13}.{LEDGER_TABLE_V13} FINAL
        WHERE import_id = {{import_id:String}}
        """,
        parameters={"import_id": import_id},
    ).result_rows
    return str(rows[0][0]) if rows else None


def _write_ledger(
    client: Any,
    *,
    import_id: str,
    contract: dict[str, Any],
    window_name: str,
    start_ns: int,
    end_ns: int,
    status: str,
    rows_seen: int,
    rows_inserted: int,
    error_message: str = "",
) -> None:
    now_ms = int(time.time() * 1000)
    client.insert(
        f"{DATABASE_V13}.{LEDGER_TABLE_V13}",
        [[
            import_id,
            contract["chain_version"],
            contract["canonical_chain_hash"],
            "BTCUSDT",
            window_name,
            start_ns,
            end_ns,
            status,
            rows_seen,
            rows_inserted,
            error_message,
            now_ms,
        ]],
        column_names=[
            "import_id",
            "chain_version",
            "canonical_chain_hash",
            "symbol",
            "window_name",
            "window_start_ns",
            "window_end_ns",
            "status",
            "rows_seen",
            "rows_inserted",
            "error_message",
            "ledger_version",
        ],
    )


def import_pilot_window(
    client: Any,
    *,
    contract: dict[str, Any],
    window_name: str,
    start_ns: int,
    end_ns: int,
) -> dict[str, Any]:
    material = {
        "schema": EVENT_SCHEMA_VERSION,
        "chain_version": contract["chain_version"],
        "chain_hash": contract["canonical_chain_hash"],
        "window_name": window_name,
        "start_ns": start_ns,
        "end_ns": end_ns,
    }
    import_id = hashlib.sha256(_canonical_json(material)).hexdigest()
    if _ledger_status(client, import_id) == "COMPLETE":
        return {
            "window_name": window_name,
            "import_id": import_id,
            "status": "SKIPPED_ALREADY_COMPLETE",
            "rows_inserted": 0,
        }

    selected = [
        row
        for row in contract["segments"]
        if row["first_event_time_ns"] < end_ns and row["last_event_time_ns"] >= start_ns
    ]
    if not selected:
        raise V13PilotError(
            f"STOP_V1_3_BRONZE_SEGMENT_MAPPING_INVALID: no segment for {window_name}"
        )
    _write_ledger(
        client,
        import_id=import_id,
        contract=contract,
        window_name=window_name,
        start_ns=start_ns,
        end_ns=end_ns,
        status="STARTED",
        rows_seen=0,
        rows_inserted=0,
    )
    rows_seen = rows_inserted = 0
    ingestion_ts_ms = int(time.time() * 1000)
    batch: list[tuple[Any, ...]] = []
    for segment in selected:
        for record, ordinal in iter_records_stream(Path(segment["source_path"])):
            event_ns = record.get("event_time_ns")
            if event_ns is None or not (start_ns <= int(event_ns) < end_ns):
                continue
            rows_seen += 1
            batch.append(
                _event_row(record, ordinal, segment, contract["chain_version"], ingestion_ts_ms)
            )
            if len(batch) >= BATCH_SIZE:
                client.insert(
                    f"{DATABASE_V13}.{EVENTS_TABLE_V13}",
                    batch,
                    column_names=EVENT_COLUMNS,
                )
                rows_inserted += len(batch)
                batch.clear()
                _check_rss()
    if batch:
        client.insert(
            f"{DATABASE_V13}.{EVENTS_TABLE_V13}",
            batch,
            column_names=EVENT_COLUMNS,
        )
        rows_inserted += len(batch)
    _write_ledger(
        client,
        import_id=import_id,
        contract=contract,
        window_name=window_name,
        start_ns=start_ns,
        end_ns=end_ns,
        status="COMPLETE",
        rows_seen=rows_seen,
        rows_inserted=rows_inserted,
    )
    return {
        "window_name": window_name,
        "import_id": import_id,
        "status": "COMPLETE",
        "rows_seen": rows_seen,
        "rows_inserted": rows_inserted,
        "segment_count": len(selected),
    }


def derive_reconnect_window(contract: dict[str, Any]) -> tuple[int, int]:
    """Derive a bounded 21Z window containing gap_marker then exchange snapshot."""
    candidates = [
        row for row in contract["segments"]
        if "20260907T210000Z" in Path(row["source_path"]).name
    ]
    for segment in candidates:
        gap_ns: int | None = None
        for record, _ in iter_records_stream(Path(segment["source_path"])):
            kind = str(record.get("message_type") or "")
            event_ns = int(record.get("event_time_ns") or record.get("receive_time_ns") or 0)
            if kind == "gap_marker":
                gap_ns = event_ns
            elif kind == "snapshot" and gap_ns is not None and event_ns >= gap_ns:
                return gap_ns - 1_000_000_000, event_ns + 1_000_000_000
    raise V13PilotError("STOP_V1_3_SEGMENT_CHAIN_AMBIGUOUS: no bounded 21Z reconnect")


def prove_window_order(
    client: Any,
    *,
    contract: dict[str, Any],
    window_name: str,
    start_ns: int,
    end_ns: int,
) -> dict[str, Any]:
    def iso(ns: int) -> str:
        sec, rem = divmod(ns, 1_000_000_000)
        dt = datetime.fromtimestamp(sec, tz=timezone.utc)
        return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{rem:09d}Z"

    raw_rows, query_s = load_bronze_window(
        client,
        symbol="BTCUSDT",
        window_start=iso(start_ns),
        window_end=iso(end_ns),
        chain_version=contract["chain_version"],
        canonical_chain_hash=contract["canonical_chain_hash"],
    )
    records = [bronze_row_from_ch(tuple(row)) for row in raw_rows]
    ordered = sort_bronze_source_order(records)
    keys = [
        (int(row.canonical_segment_chain_index), row.record_ordinal)
        for row in ordered
    ]
    if records != ordered or keys != sorted(keys) or len(keys) != len(set(
        (row.source_segment_sha256, row.record_ordinal) for row in ordered
    )):
        raise V13PilotError("STOP_V1_3_SILVER_ORDER_NOT_PROVEN: query/apply order mismatch")
    bad_mapping = client.query(
        f"""
        SELECT count()
        FROM {DATABASE_V13}.{EVENTS_TABLE_V13} AS e FINAL
        LEFT JOIN {DATABASE_V13}.{SEGMENTS_TABLE_V13} AS s FINAL
          ON e.symbol = s.symbol
         AND e.chain_version = s.chain_version
         AND e.source_segment_sha256 = s.source_segment_sha256
        WHERE e.symbol = 'BTCUSDT'
          AND e.chain_version = {{chain_version:String}}
          AND e.event_time_ns >= {{start_ns:UInt64}}
          AND e.event_time_ns < {{end_ns:UInt64}}
          AND (
            s.source_segment_sha256 = ''
            OR s.is_canonical != 1
            OR e.canonical_segment_chain_index != s.canonical_segment_chain_index
          )
        """,
        parameters={
            "chain_version": contract["chain_version"],
            "start_ns": start_ns,
            "end_ns": end_ns,
        },
    ).result_rows[0][0]
    if int(bad_mapping):
        raise V13PilotError("STOP_V1_3_BRONZE_SEGMENT_MAPPING_INVALID: join mismatch")
    return {
        "window_name": window_name,
        "row_count": len(records),
        "segment_ranks": sorted({key[0] for key in keys}),
        "first_apply_key": list(keys[0]) if keys else None,
        "last_apply_key": list(keys[-1]) if keys else None,
        "query_s": round(query_s, 6),
        "mapping_mismatch_count": int(bad_mapping),
        "order_proven": True,
    }


def prove_episode1(client: Any, contract: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    prefix_start = iso_to_ns_exact("2026-09-06T20:14:59.873000000Z")
    analysis_start = iso_to_ns_exact("2026-09-06T20:19:00.000000000Z")
    end = iso_to_ns_exact("2026-09-06T20:20:00.000000000Z")
    raw_rows, _ = load_bronze_window(
        client,
        symbol="BTCUSDT",
        window_start="2026-09-06T20:14:59.873000000Z",
        window_end="2026-09-06T20:20:00.000000000Z",
        chain_version=contract["chain_version"],
        canonical_chain_hash=contract["canonical_chain_hash"],
    )
    records = sort_bronze_source_order([bronze_row_from_ch(tuple(row)) for row in raw_rows])
    replay = replay_bronze_to_silver(
        rows=records,
        symbol="BTCUSDT",
        window_start_ns=analysis_start,
        window_end_ns=end,
        silver_build_id="0" * 64,
        created_at_ms=0,
    )
    canonical_rows = [
        canonical_lc_row(
            event_time_ns=row["event_time_ns"],
            update_id=row["update_id"],
            seq=row["seq"],
            side=row["side"],
            price=row["price"],
            old_size=row["old_size"],
            new_size=row["new_size"],
            change_type=row["change_type"],
        )
        for row in replay.level_changes
    ]
    actual_hash = hash_canonical_lcs(canonical_rows)
    reference = json.loads((run_dir / "detail_parity_report.json").read_text())
    expected_hash = reference["level_change_parity"]["reference_hash"]
    dt_bad = client.query(
        f"""
        SELECT count()
        FROM {DATABASE_V13}.{EVENTS_TABLE_V13} FINAL
        WHERE chain_version = {{chain_version:String}}
          AND event_time_ns >= {{start_ns:UInt64}}
          AND event_time_ns < {{end_ns:UInt64}}
          AND (
            toUnixTimestamp64Nano(event_time) != event_time_ns
            OR toUnixTimestamp64Nano(receive_time) != receive_time_ns
          )
        """,
        parameters={
            "chain_version": contract["chain_version"],
            "start_ns": prefix_start,
            "end_ns": end,
        },
    ).result_rows[0][0]
    proof = {
        "level_changes_actual": len(replay.level_changes),
        "level_changes_expected": 30_939,
        "actual_lc_hash": actual_hash,
        "expected_lc_hash": expected_hash,
        "states_100ms_actual": len(replay.metrics),
        "states_100ms_expected": 600,
        "utc_ns_mismatch_count": int(dt_bad),
    }
    proof["status"] = (
        "PARITY_EXACT"
        if proof["level_changes_actual"] == proof["level_changes_expected"]
        and actual_hash == expected_hash
        and proof["states_100ms_actual"] == 600
        and int(dt_bad) == 0
        else "STOP_V1_3_EPISODE1_PARITY_MISMATCH"
    )
    if proof["status"] != "PARITY_EXACT":
        raise V13PilotError(str(proof["status"]))
    return proof


def run_v13_pilot(
    *,
    run_dir: Path,
    client: Any | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    own_client = client is None
    client = client or get_clickhouse_client()
    try:
        ensure_v13_schema(client)
        contract = build_chain_contract(run_dir / "btc_segment_order_contract_v1.json")
        chain_registration = register_chain(client, contract)
        reconnect_start, reconnect_end = derive_reconnect_window(contract)
        windows = [
            (
                "episode1",
                iso_to_ns_exact("2026-09-06T20:14:59.873000000Z"),
                iso_to_ns_exact("2026-09-06T20:20:00.000000000Z"),
            ),
            (
                "multiple_hour_2026_09_06_07",
                iso_to_ns_exact("2026-09-06T07:00:00.000000000Z"),
                iso_to_ns_exact("2026-09-06T08:00:00.000000000Z"),
            ),
            (
                "instance_change_2026_09_10_09",
                iso_to_ns_exact("2026-09-10T09:00:00.000000000Z"),
                iso_to_ns_exact("2026-09-10T10:00:00.000000000Z"),
            ),
            ("reconnect_2026_09_07_21", reconnect_start, reconnect_end),
        ]
        imports = []
        proofs = []
        for index, (name, start_ns, end_ns) in enumerate(windows, 1):
            print(f"pilot progress {index}/4 window={name}", flush=True)
            imports.append(
                import_pilot_window(
                    client,
                    contract=contract,
                    window_name=name,
                    start_ns=start_ns,
                    end_ns=end_ns,
                )
            )
            proofs.append(
                prove_window_order(
                    client,
                    contract=contract,
                    window_name=name,
                    start_ns=start_ns,
                    end_ns=end_ns,
                )
            )
            _check_rss()
        episode1 = prove_episode1(client, contract, run_dir)
        second_chain_registration = register_chain(client, contract)
        second_imports = [
            import_pilot_window(
                client,
                contract=contract,
                window_name=name,
                start_ns=start_ns,
                end_ns=end_ns,
            )
            for name, start_ns, end_ns in windows
        ]
        second_proofs = [
            prove_window_order(
                client,
                contract=contract,
                window_name=name,
                start_ns=start_ns,
                end_ns=end_ns,
            )
            for name, start_ns, end_ns in windows
        ]

        def stable_proofs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                {key: value for key, value in row.items() if key != "query_s"}
                for row in rows
            ]

        first_result_hash = hashlib.sha256(_canonical_json({
            "chain_version": contract["chain_version"],
            "chain_hash": contract["canonical_chain_hash"],
            "proofs": stable_proofs(proofs),
            "episode1": episode1,
        })).hexdigest()
        second_result_hash = hashlib.sha256(_canonical_json({
            "chain_version": contract["chain_version"],
            "chain_hash": contract["canonical_chain_hash"],
            "proofs": stable_proofs(second_proofs),
            "episode1": prove_episode1(client, contract, run_dir),
        })).hexdigest()
        if first_result_hash != second_result_hash:
            raise V13PilotError(
                "STOP_V1_3_SILVER_ORDER_NOT_PROVEN: repeated canonical result hash changed"
            )
        result = {
            "verdict": "CLICKHOUSE_V1_3_CANONICAL_SEGMENT_ORDER_PILOT_PROVEN",
            "root_cause": (
                "v1.2 had only segment SHA identity and in-segment ordinal; "
                "v1.3 persists canonical rank"
            ),
            "database": DATABASE_V13,
            "chain_version": contract["chain_version"],
            "canonical_chain_hash": contract["canonical_chain_hash"],
            "chain_segment_count": len(contract["segments"]),
            "chain_registration": chain_registration,
            "second_chain_registration": second_chain_registration,
            "pilot_imports": imports,
            "pilot_order_proofs": proofs,
            "second_imports": second_imports,
            "second_order_proofs": second_proofs,
            "determinism": {
                "first_canonical_result_hash": first_result_hash,
                "second_canonical_result_hash": second_result_hash,
                "identical": True,
            },
            "episode1": episode1,
            "resource_usage": {
                "elapsed_s": round(time.monotonic() - started, 3),
                "peak_rss_kb": _rss_kb(),
                "worker_count": 1,
            },
        }
        return result
    finally:
        if own_client:
            client.close()


def write_report(result: dict[str, Any], path: Path) -> None:
    imports = "\n".join(
        f"- {row['window_name']}: {row['status']}, rows={row.get('rows_inserted', 0)}"
        for row in result["pilot_imports"]
    )
    proofs = "\n".join(
        f"- {row['window_name']}: rows={row['row_count']}, "
        f"ranks={row['segment_ranks']}, order_proven={row['order_proven']}"
        for row in result["pilot_order_proofs"]
    )
    second = "\n".join(
        f"- {row['window_name']}: {row['status']}" for row in result["second_imports"]
    )
    ep = result["episode1"]
    path.write_text(
        f"""# ClickHouse v1.3 Canonical Segment Order Pilot

## 1. VERDICT

`{result['verdict']}`

## 2. Root Cause

{result['root_cause']}

## 3. Betroffene Dateien/Funktionen

- `canonical_segment_order_v1_3.py`: v1.3 schema, chain registry, Bronze pilot importer.
- `silver_replay.sort_bronze_source_order`: canonical rank before ordinal.
- `silver_builder.load_bronze_window`: metadata join and canonical SQL order.
- `gap_semantics_audit.list_closed_btc_segments`: temporal discovery order.

## 4. v1.3-Schema

Database `{result['database']}` with canonical segments, Bronze events, import ledger,
and versioned Silver pilot tables. All canonical times are integer ns; DateTime64
columns are materialized with `fromUnixTimestamp64Nano`.

## 5–6. Canonical contract, chain version/hash

- chain_version: `{result['chain_version']}`
- canonical_chain_hash: `{result['canonical_chain_hash']}`
- segments: {result['chain_segment_count']}
- registration: {result['chain_registration']['status']}

## 7. Mehrfachstunden

The previously proven five repeated UTC slots remain sequential canonical shards;
no SHA or path ordering is used.

## 8. Bronze-Zuordnung

Events carry chain version, canonical rank, segment SHA identity and record ordinal.
The Silver metadata join reports zero mismatches.

## 9–10. Silver-Reihenfolge und Pilotfenster

`(canonical_segment_chain_index, record_ordinal)`

{imports}

{proofs}

## 11. Episode-1-Parität

- Level changes: {ep['level_changes_actual']}/{ep['level_changes_expected']}
- LC hash exact: {ep['actual_lc_hash'] == ep['expected_lc_hash']}
- states_100ms: {ep['states_100ms_actual']}/{ep['states_100ms_expected']}
- UTC/ns mismatches: {ep['utc_ns_mismatch_count']}
- status: `{ep['status']}`

## 12. Idempotenz

- Chain second registration: `{result['second_chain_registration']['status']}`
{second}
- First canonical result hash: `{result['determinism']['first_canonical_result_hash']}`
- Second canonical result hash: `{result['determinism']['second_canonical_result_hash']}`
- Identical: {result['determinism']['identical']}

## 13. Tests

The final execution summary records the split test-suite totals. Schema, rank
mapping, missing/duplicate-rank stops, stability, SQL ordering, cross-segment
epoch behavior, idempotence, UTC/ns and Episode-1 regressions are covered.

## 14. Laufzeit und Peak RSS

- elapsed: {result['resource_usage']['elapsed_s']} s
- peak RSS: {result['resource_usage']['peak_rss_kb']} KiB
- workers: 1

## 15–17. Git, Risiken, Empfehlung

No commit/push and no Full import/build. Existing v1/v1.2 ClickHouse tables were
not modified. v1.3 Silver tables remain empty. Only four bounded windows were
imported; review this contract before a separately authorized full Bronze import.
""",
        encoding="utf-8",
    )


def main() -> int:
    run_dir = Path(
        "obfull_research_engine/runs/clickhouse_research_store_pilot_v1"
    )
    try:
        result = run_v13_pilot(run_dir=run_dir)
        write_report(
            result,
            run_dir / "CLICKHOUSE_V1_3_CANONICAL_SEGMENT_ORDER_PILOT.md",
        )
        (run_dir / "clickhouse_v1_3_canonical_segment_order_pilot.json").write_text(
            json.dumps(result, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(result, indent=2))
        return 0
    except V13PilotError as exc:
        print(json.dumps({"verdict": str(exc).split(":", 1)[0], "error": str(exc)}, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Guarded, resumable CLI for a separately authorized BTC Bronze v1.3 import.

This module is intentionally inert unless ``--run`` is supplied. ``--check-only``
and ``--verify-only`` never execute DDL or DML.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import resource
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .canonical_segment_order_v1_3 import (
    BATCH_SIZE,
    DATABASE_V13 as SOURCE_DATABASE,
    EVENT_COLUMNS,
    SEGMENTS_TABLE_V13 as SOURCE_SEGMENTS_TABLE,
    _canonical_json,
    _event_row,
)
from .gap_semantics_audit import iter_records_stream
from .helpers import (
    current_rss_bytes,
    get_clickhouse_client,
    load_manifest,
    verify_manifest_against_segment,
)

EXPECTED_BRANCH = "research/clickhouse-defense-store-v1"
EXPECTED_SEGMENT_COUNT = 164
DEFAULT_DATABASE = "research_full_ob_continuous_v1_3"
DEFAULT_LOCK_PATH = Path(
    "/home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/"
    "obfull_research_engine/runs/bronze_full_import_v1_3/import.lock"
)
SEGMENTS_TABLE = "canonical_segments_v1_3"
EVENTS_TABLE = "raw_full_ob_events_v1_3"
LEDGER_TABLE = "raw_full_ob_import_segments_v1_3"
RUNNER_VERSION = "btc_bronze_full_import_runner_v1_3"
DEFAULT_MAX_RSS_MIB = 1536
DEFAULT_MIN_FREE_DISK_GIB = 200.0
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class BronzeImportError(RuntimeError):
    """Fail-closed Bronze runner error with a stable STOP verdict."""


class ControlledInterrupt(BronzeImportError):
    """Raised after SIGINT/SIGTERM has requested a controlled stop."""


@dataclass(frozen=True)
class ImportConfig:
    symbol: str
    chain_version: str
    expected_chain_hash: str
    archive_root: Path
    database: str
    resume: bool
    start_chain_index: int
    end_chain_index: int
    max_rss_mib: int
    min_free_disk_gib: float
    progress_every_segments: int
    report_path: Path
    lock_path: Path
    expected_segment_count: int = EXPECTED_SEGMENT_COUNT
    enforce_canonical_lock_path: bool = True


@dataclass
class SegmentSpec:
    symbol: str
    chain_version: str
    canonical_segment_chain_index: int
    source_segment_sha256: str
    source_path: str
    segment_start_ns: int
    segment_end_ns: int
    first_archive_time_ns: int
    last_archive_time_ns: int
    first_event_time_ns: int
    last_event_time_ns: int
    first_receive_time_ns: int
    last_receive_time_ns: int
    archive_instance_id: str
    record_count: int
    resolution_status: str
    is_canonical: int
    predecessor_segment_sha256: str
    anchor_type: str
    anchor_provenance: str
    continuity_status: str
    canonical_chain_hash: str

    def contract_payload(self) -> dict[str, Any]:
        row = asdict(self)
        row.pop("chain_version")
        row.pop("canonical_chain_hash")
        row.pop("source_path")
        return row


SEGMENT_SELECT_COLUMNS = (
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
)


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, (bytes, bytearray)) else str(value)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _ns_iso(ns: int) -> str:
    seconds, remainder = divmod(int(ns), 1_000_000_000)
    dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{remainder:09d}Z"


def _sha_text(value: str, *, label: str) -> str:
    normalized = str(value).lower()
    if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
        raise BronzeImportError(f"STOP_BRONZE_RUNNER_SAFETY_{label}_INVALID")
    return normalized


def validate_target_database(database: str) -> None:
    if not _IDENTIFIER.fullmatch(database):
        raise BronzeImportError("STOP_BRONZE_RUNNER_SAFETY_DATABASE_IDENTIFIER")
    if database != DEFAULT_DATABASE:
        raise BronzeImportError("STOP_BRONZE_RUNNER_SAFETY_DATABASE_NOT_PRODUCTION_V1_3")


def code_identity(repo_root: Path | None = None) -> dict[str, str]:
    root = repo_root or Path(__file__).resolve().parents[4]

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", *args], cwd=root, text=True, stderr=subprocess.DEVNULL
        ).strip()

    try:
        return {"branch": git("branch", "--show-current"), "head": git("rev-parse", "HEAD")}
    except (OSError, subprocess.CalledProcessError):
        return {"branch": "UNKNOWN", "head": "UNKNOWN"}


def _table_ddls(database: str) -> tuple[str, ...]:
    validate_target_database(database)
    return (
        f"CREATE DATABASE IF NOT EXISTS {database}",
        f"""
        CREATE TABLE IF NOT EXISTS {database}.{SEGMENTS_TABLE}
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
        SETTINGS index_granularity = 8192
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {database}.{EVENTS_TABLE}
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
        ORDER BY (
            symbol, chain_version, canonical_segment_chain_index,
            record_ordinal, record_id
        )
        SETTINGS index_granularity = 8192
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {database}.{LEDGER_TABLE}
        (
            segment_import_id FixedString(64),
            runner_version String,
            chain_version String,
            canonical_chain_hash FixedString(64),
            symbol LowCardinality(String),
            canonical_segment_chain_index UInt64,
            source_segment_sha256 FixedString(64),
            status LowCardinality(String),
            records_expected UInt64,
            records_seen UInt64,
            rows_inserted UInt64,
            error_message String,
            host String,
            pid UInt64,
            ledger_version_ms UInt64,
            updated_at DateTime64(3, 'UTC')
                MATERIALIZED fromUnixTimestamp64Milli(ledger_version_ms, 'UTC')
        )
        ENGINE = ReplacingMergeTree(ledger_version_ms)
        ORDER BY segment_import_id
        SETTINGS index_granularity = 8192
        """.strip(),
    )


def init_schema(client: Any, database: str) -> None:
    client.command("SET max_threads = 1")
    for ddl in _table_ddls(database):
        client.command(ddl)


def load_source_contract(client: Any, config: ImportConfig) -> list[SegmentSpec]:
    rows = client.query(
        f"""
        SELECT {", ".join(SEGMENT_SELECT_COLUMNS)}
        FROM {SOURCE_DATABASE}.{SOURCE_SEGMENTS_TABLE} FINAL
        WHERE symbol = {{symbol:String}}
          AND chain_version = {{chain_version:String}}
          AND canonical_chain_hash = {{chain_hash:String}}
          AND is_canonical = 1
        ORDER BY canonical_segment_chain_index
        """,
        parameters={
            "symbol": config.symbol,
            "chain_version": config.chain_version,
            "chain_hash": config.expected_chain_hash,
        },
    ).result_rows
    segments = [
        SegmentSpec(
            **{
                name: (
                    _text(value)
                    if name
                    in {
                        "symbol",
                        "chain_version",
                        "source_segment_sha256",
                        "source_path",
                        "archive_instance_id",
                        "resolution_status",
                        "predecessor_segment_sha256",
                        "anchor_type",
                        "anchor_provenance",
                        "continuity_status",
                        "canonical_chain_hash",
                    }
                    else int(value)
                )
                for name, value in zip(SEGMENT_SELECT_COLUMNS, row)
            }
        )
        for row in rows
    ]
    validate_contract(segments, config)
    return segments


def validate_contract(segments: Sequence[SegmentSpec], config: ImportConfig) -> None:
    if config.symbol != "BTCUSDT":
        raise BronzeImportError("STOP_BRONZE_RUNNER_SAFETY_SYMBOL_NOT_BTCUSDT")
    _sha_text(config.expected_chain_hash, label="CHAIN_HASH")
    if len(segments) != config.expected_segment_count:
        raise BronzeImportError(
            "STOP_BRONZE_RUNNER_SAFETY_SEGMENT_COUNT_"
            f"{len(segments)}_EXPECTED_{config.expected_segment_count}"
        )
    indices = [row.canonical_segment_chain_index for row in segments]
    if indices != list(range(config.expected_segment_count)):
        raise BronzeImportError("STOP_BRONZE_RUNNER_SAFETY_SEGMENT_RANKS_INVALID")
    shas = [row.source_segment_sha256 for row in segments]
    if len(set(shas)) != len(shas):
        raise BronzeImportError("STOP_BRONZE_RUNNER_SAFETY_DUPLICATE_SEGMENT_SHA")
    for row in segments:
        _sha_text(row.source_segment_sha256, label="SEGMENT_SHA")
        if (
            row.symbol != config.symbol
            or row.chain_version != config.chain_version
            or row.canonical_chain_hash != config.expected_chain_hash
            or not row.is_canonical
        ):
            raise BronzeImportError("STOP_BRONZE_RUNNER_SAFETY_CHAIN_MAPPING_INVALID")
    calculated = hashlib.sha256(
        _canonical_json([row.contract_payload() for row in segments])
    ).hexdigest()
    if calculated != config.expected_chain_hash:
        raise BronzeImportError("STOP_BRONZE_RUNNER_SAFETY_CHAIN_HASH_MISMATCH")


def resolve_archive_path(segment: SegmentSpec, archive_root: Path) -> Path:
    original = Path(segment.source_path)
    try:
        relative = original.relative_to(original.parents[4])
    except (ValueError, IndexError):
        relative = Path(segment.symbol) / original.name
    if relative.parts[0] != segment.symbol:
        marker = original.parts.index(segment.symbol) if segment.symbol in original.parts else -1
        relative = (
            Path(*original.parts[marker:])
            if marker >= 0
            else Path(segment.symbol) / original.name
        )
    candidate = (archive_root / relative).resolve()
    root = archive_root.resolve()
    if root not in candidate.parents:
        raise BronzeImportError("STOP_BRONZE_RUNNER_SAFETY_ARCHIVE_PATH_ESCAPE")
    return candidate


def verify_archives(
    segments: Sequence[SegmentSpec],
    config: ImportConfig,
    *,
    verify_sha: bool = True,
    stop: StopState | None = None,
) -> list[SegmentSpec]:
    verified: list[SegmentSpec] = []
    for segment in segments:
        if stop is not None:
            stop.check()
        path = resolve_archive_path(segment, config.archive_root)
        if not path.is_file():
            raise BronzeImportError(
                f"STOP_BRONZE_RUNNER_SAFETY_ARCHIVE_MISSING_INDEX_{segment.canonical_segment_chain_index}"
            )
        manifest = load_manifest(path)
        status_values = {
            str(manifest.get(key) or "").upper()
            for key in ("status", "completion_status", "state")
            if manifest.get(key) is not None
        }
        if (
            any("LIVE_OPEN" in value for value in status_values)
            or "LIVE_OPEN" in path.name.upper()
        ):
            raise BronzeImportError("STOP_BRONZE_RUNNER_SAFETY_LIVE_OPEN_INCLUDED")
        if any(
            value not in {"CLOSED", "COMPLETE", "GAP"}
            for value in status_values
        ):
            raise BronzeImportError(
                "STOP_BRONZE_RUNNER_SAFETY_CONTRADICTORY_SEGMENT_STATUS"
            )
        completion_status = str(manifest.get("completion_status") or "").upper()
        if completion_status not in {"COMPLETE", "GAP"}:
            raise BronzeImportError(
                "STOP_BRONZE_RUNNER_SAFETY_SEGMENT_NOT_EXPLICITLY_CLOSED"
            )
        manifest_sha = str(manifest.get("segment_sha256") or "").lower()
        if manifest_sha != segment.source_segment_sha256:
            raise BronzeImportError(
                "STOP_BRONZE_RUNNER_SAFETY_SEGMENT_MANIFEST_MAPPING_MISMATCH"
            )
        if verify_sha and verify_manifest_against_segment(path, manifest) != segment.source_segment_sha256:
            raise BronzeImportError("STOP_BRONZE_RUNNER_SAFETY_SEGMENT_SHA_MISMATCH")
        if stop is not None:
            stop.check()
        verified.append(SegmentSpec(**{**asdict(segment), "source_path": str(path)}))
    return verified


def clickhouse_free_bytes(client: Any) -> int:
    rows = client.query(
        "SELECT min(free_space) FROM system.disks WHERE type != 'ObjectStorage'"
    ).result_rows
    if not rows or rows[0][0] is None:
        raise BronzeImportError("STOP_BRONZE_RUNNER_SAFETY_DISK_SPACE_UNKNOWN")
    return int(rows[0][0])


def _check_resources(client: Any, config: ImportConfig) -> tuple[int, int]:
    if config.max_rss_mib <= 0 or config.min_free_disk_gib <= 0:
        raise BronzeImportError("STOP_BRONZE_RUNNER_SAFETY_RESOURCE_LIMIT_INVALID")
    rss = current_rss_bytes()
    max_rss = int(config.max_rss_mib) * 1024 * 1024
    if rss > max_rss:
        raise BronzeImportError("STOP_BRONZE_RUNNER_SAFETY_RSS_LIMIT")
    free = clickhouse_free_bytes(client)
    if free < int(config.min_free_disk_gib * 1024**3):
        raise BronzeImportError("STOP_BRONZE_RUNNER_SAFETY_FREE_DISK_LIMIT")
    return rss, free


def target_schema_exists(client: Any, database: str) -> bool:
    count = client.query(
        """
        SELECT count()
        FROM system.tables
        WHERE database = {database:String}
          AND name IN {tables:Array(String)}
        """,
        parameters={
            "database": database,
            "tables": [SEGMENTS_TABLE, EVENTS_TABLE, LEDGER_TABLE],
        },
    ).result_rows[0][0]
    return int(count) == 3


def _schema_contract() -> tuple[
    dict[str, tuple[str, str, str, str, str]],
    dict[str, list[tuple[str, str, str, str]]],
]:
    plain = lambda name, kind: (name, kind, "", "")
    materialized = lambda name, kind, expression: (
        name,
        kind,
        "MATERIALIZED",
        expression,
    )
    tables = {
        SEGMENTS_TABLE: (
            "ReplacingMergeTree",
            "ReplacingMergeTree(created_at_ms) ORDER BY "
            "(symbol, chain_version, source_segment_sha256) "
            "SETTINGS index_granularity = 8192",
            "",
            "symbol, chain_version, source_segment_sha256",
            "symbol, chain_version, source_segment_sha256",
        ),
        EVENTS_TABLE: (
            "ReplacingMergeTree",
            "ReplacingMergeTree(ingestion_ts_ms) PARTITION BY "
            "(symbol, toYYYYMMDD(event_time)) ORDER BY "
            "(symbol, chain_version, canonical_segment_chain_index, "
            "record_ordinal, record_id) SETTINGS index_granularity = 8192",
            "(symbol, toYYYYMMDD(event_time))",
            "symbol, chain_version, canonical_segment_chain_index, record_ordinal, record_id",
            "symbol, chain_version, canonical_segment_chain_index, record_ordinal, record_id",
        ),
        LEDGER_TABLE: (
            "ReplacingMergeTree",
            "ReplacingMergeTree(ledger_version_ms) ORDER BY segment_import_id "
            "SETTINGS index_granularity = 8192",
            "",
            "segment_import_id",
            "segment_import_id",
        ),
    }
    columns = {
        SEGMENTS_TABLE: [
            plain("symbol", "LowCardinality(String)"),
            plain("chain_version", "String"),
            plain("canonical_segment_chain_index", "UInt64"),
            plain("source_segment_sha256", "FixedString(64)"),
            plain("source_path", "String"),
            plain("segment_start_ns", "UInt64"),
            materialized(
                "segment_start",
                "DateTime64(9, 'UTC')",
                "fromUnixTimestamp64Nano(segment_start_ns, 'UTC')",
            ),
            plain("segment_end_ns", "UInt64"),
            materialized(
                "segment_end",
                "DateTime64(9, 'UTC')",
                "fromUnixTimestamp64Nano(segment_end_ns, 'UTC')",
            ),
            *[
                plain(name, "UInt64")
                for name in (
                    "first_archive_time_ns",
                    "last_archive_time_ns",
                    "first_event_time_ns",
                    "last_event_time_ns",
                    "first_receive_time_ns",
                    "last_receive_time_ns",
                )
            ],
            plain("archive_instance_id", "String"),
            plain("record_count", "UInt64"),
            plain("resolution_status", "LowCardinality(String)"),
            plain("is_canonical", "UInt8"),
            plain("predecessor_segment_sha256", "String"),
            plain("anchor_type", "LowCardinality(String)"),
            plain("anchor_provenance", "String"),
            plain("continuity_status", "LowCardinality(String)"),
            plain("canonical_chain_hash", "FixedString(64)"),
            plain("created_at_ms", "UInt64"),
            materialized(
                "created_at",
                "DateTime64(3, 'UTC')",
                "fromUnixTimestamp64Milli(created_at_ms, 'UTC')",
            ),
        ],
        EVENTS_TABLE: [
            plain("record_id", "FixedString(64)"),
            plain("symbol", "LowCardinality(String)"),
            plain("chain_version", "String"),
            plain("canonical_segment_chain_index", "UInt64"),
            plain("event_time_ns", "UInt64"),
            materialized(
                "event_time",
                "DateTime64(9, 'UTC')",
                "fromUnixTimestamp64Nano(event_time_ns, 'UTC')",
            ),
            plain("receive_time_ns", "UInt64"),
            materialized(
                "receive_time",
                "DateTime64(9, 'UTC')",
                "fromUnixTimestamp64Nano(receive_time_ns, 'UTC')",
            ),
            plain("message_type", "LowCardinality(String)"),
            plain("update_id", "UInt64"),
            plain("seq", "UInt64"),
            plain("update_id_present", "UInt8"),
            plain("seq_present", "UInt8"),
            plain("event_time_present", "UInt8"),
            plain("receive_time_present", "UInt8"),
            plain("source_path", "String"),
            plain("source_segment_sha256", "FixedString(64)"),
            plain("record_ordinal", "UInt64"),
            plain("payload_sha256", "FixedString(64)"),
            plain("original_payload", "String"),
            plain("envelope_version", "String"),
            plain("ingestion_ts_ms", "UInt64"),
            materialized(
                "ingestion_ts",
                "DateTime64(3, 'UTC')",
                "fromUnixTimestamp64Milli(ingestion_ts_ms, 'UTC')",
            ),
        ],
        LEDGER_TABLE: [
            plain("segment_import_id", "FixedString(64)"),
            plain("runner_version", "String"),
            plain("chain_version", "String"),
            plain("canonical_chain_hash", "FixedString(64)"),
            plain("symbol", "LowCardinality(String)"),
            plain("canonical_segment_chain_index", "UInt64"),
            plain("source_segment_sha256", "FixedString(64)"),
            plain("status", "LowCardinality(String)"),
            plain("records_expected", "UInt64"),
            plain("records_seen", "UInt64"),
            plain("rows_inserted", "UInt64"),
            plain("error_message", "String"),
            plain("host", "String"),
            plain("pid", "UInt64"),
            plain("ledger_version_ms", "UInt64"),
            materialized(
                "updated_at",
                "DateTime64(3, 'UTC')",
                "fromUnixTimestamp64Milli(ledger_version_ms, 'UTC')",
            ),
        ],
    }
    return tables, columns


def _normalized_schema_text(value: Any) -> str:
    return "".join(_text(value).replace("`", "").split())


def validate_schema_metadata(
    table_rows: Sequence[Sequence[Any]], column_rows: Sequence[Sequence[Any]]
) -> None:
    expected_tables, expected_columns = _schema_contract()
    actual_tables = {
        _text(row[0]): (
            _text(row[1]),
            _normalized_schema_text(row[2]),
            _normalized_schema_text(row[3]),
            _normalized_schema_text(row[4]),
            _normalized_schema_text(row[5]),
        )
        for row in table_rows
    }
    normalized_expected_tables = {
        table: (
            engine,
            _normalized_schema_text(engine_full),
            _normalized_schema_text(partition),
            _normalized_schema_text(order),
            _normalized_schema_text(primary),
        )
        for table, (engine, engine_full, partition, order, primary) in expected_tables.items()
    }
    actual_columns: dict[str, list[tuple[str, str, str, str]]] = {
        table: [] for table in expected_columns
    }
    for table, name, kind, default_kind, expression in column_rows:
        if _text(table) in actual_columns:
            actual_columns[_text(table)].append(
                (
                    _text(name),
                    _normalized_schema_text(kind),
                    _text(default_kind),
                    _normalized_schema_text(expression),
                )
            )
    normalized_expected_columns = {
        table: [
            (
                name,
                _normalized_schema_text(kind),
                default_kind,
                _normalized_schema_text(expression),
            )
            for name, kind, default_kind, expression in rows
        ]
        for table, rows in expected_columns.items()
    }
    if (
        actual_tables != normalized_expected_tables
        or actual_columns != normalized_expected_columns
    ):
        raise BronzeImportError("STOP_BRONZE_RUNNER_SAFETY_TARGET_SCHEMA_MISMATCH")


def validate_target_schema(client: Any, database: str) -> None:
    table_rows = client.query(
        """
        SELECT name, engine, engine_full, partition_key, sorting_key, primary_key
        FROM system.tables
        WHERE database = {database:String}
          AND name IN {tables:Array(String)}
        ORDER BY name
        """,
        parameters={
            "database": database,
            "tables": [SEGMENTS_TABLE, EVENTS_TABLE, LEDGER_TABLE],
        },
    ).result_rows
    column_rows = client.query(
        """
        SELECT table, name, type, default_kind, default_expression
        FROM system.columns
        WHERE database = {database:String}
          AND table IN {tables:Array(String)}
        ORDER BY table, position
        """,
        parameters={
            "database": database,
            "tables": [SEGMENTS_TABLE, EVENTS_TABLE, LEDGER_TABLE],
        },
    ).result_rows
    validate_schema_metadata(table_rows, column_rows)


def _segment_import_id(config: ImportConfig, segment: SegmentSpec) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "runner_version": RUNNER_VERSION,
                "database": config.database,
                "chain_version": config.chain_version,
                "canonical_chain_hash": config.expected_chain_hash,
                "symbol": config.symbol,
                "chain_index": segment.canonical_segment_chain_index,
                "segment_sha": segment.source_segment_sha256,
            }
        )
    ).hexdigest()


def _ledger_state(
    client: Any, database: str, import_id: str
) -> tuple[str, int] | None:
    rows = client.query(
        f"""
        SELECT status, ledger_version_ms
        FROM {database}.{LEDGER_TABLE} FINAL
        WHERE segment_import_id = {{import_id:String}}
        """,
        parameters={"import_id": import_id},
    ).result_rows
    return (_text(rows[0][0]), int(rows[0][1])) if rows else None


def _ledger_status(client: Any, database: str, import_id: str) -> str | None:
    state = _ledger_state(client, database, import_id)
    return state[0] if state else None


def _write_ledger(
    client: Any,
    config: ImportConfig,
    segment: SegmentSpec,
    *,
    status: str,
    records_seen: int,
    rows_inserted: int,
    error_message: str = "",
) -> None:
    import_id = _segment_import_id(config, segment)
    prior = _ledger_state(client, config.database, import_id)
    ledger_version_ms = max(
        int(time.time() * 1000),
        (prior[1] + 1) if prior is not None else 0,
    )
    client.insert(
        f"{config.database}.{LEDGER_TABLE}",
        [
            [
                import_id,
                RUNNER_VERSION,
                config.chain_version,
                config.expected_chain_hash,
                config.symbol,
                segment.canonical_segment_chain_index,
                segment.source_segment_sha256,
                status,
                segment.record_count,
                records_seen,
                rows_inserted,
                error_message[:4096],
                socket.gethostname(),
                os.getpid(),
                ledger_version_ms,
            ]
        ],
        column_names=[
            "segment_import_id",
            "runner_version",
            "chain_version",
            "canonical_chain_hash",
            "symbol",
            "canonical_segment_chain_index",
            "source_segment_sha256",
            "status",
            "records_expected",
            "records_seen",
            "rows_inserted",
            "error_message",
            "host",
            "pid",
            "ledger_version_ms",
        ],
    )


def _target_segment_counts(
    client: Any, config: ImportConfig, segment: SegmentSpec
) -> tuple[int, int, int, int, int, int]:
    row = client.query(
        f"""
        SELECT
          count(),
          uniqExact(record_id),
          uniqExact(record_ordinal),
          countIf(
            record_id != lower(hex(SHA256(concat(
              toString(source_segment_sha256), ':', toString(record_ordinal)
            ))))
          ),
          if(count() = 0, 0, min(record_ordinal)),
          if(count() = 0, 0, max(record_ordinal))
        FROM {config.database}.{EVENTS_TABLE} FINAL
        WHERE symbol = {{symbol:String}}
          AND chain_version = {{chain_version:String}}
          AND canonical_segment_chain_index = {{chain_index:UInt64}}
          AND source_segment_sha256 = {{segment_sha:String}}
        """,
        parameters={
            "symbol": config.symbol,
            "chain_version": config.chain_version,
            "chain_index": segment.canonical_segment_chain_index,
            "segment_sha": segment.source_segment_sha256,
        },
    ).result_rows[0]
    return tuple(int(value) for value in row)  # type: ignore[return-value]


def _segment_is_complete(client: Any, config: ImportConfig, segment: SegmentSpec) -> bool:
    if _ledger_status(client, config.database, _segment_import_id(config, segment)) != "COMPLETE":
        return False
    counts = _target_segment_counts(client, config, segment)
    if counts != (
        segment.record_count,
        segment.record_count,
        segment.record_count,
        0,
        1,
        segment.record_count,
    ):
        raise BronzeImportError("STOP_BRONZE_RUNNER_RESUME_COMPLETE_SEGMENT_COUNT_MISMATCH")
    return True


@dataclass
class StopState:
    requested: bool = False
    signal_number: int = 0

    def request(self, signum: int) -> None:
        self.requested = True
        self.signal_number = int(signum)

    def check(self) -> None:
        if self.requested:
            raise ControlledInterrupt(
                f"STOP_BRONZE_IMPORT_INTERRUPTED_SIGNAL_{self.signal_number}"
            )


class ImportLock:
    def __init__(self, path: Path, metadata: dict[str, Any]):
        self.path = path
        self.metadata = metadata
        self.acquired = False
        self.token = hashlib.sha256(os.urandom(32)).hexdigest()

    @staticmethod
    def read(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BronzeImportError(
                f"STOP_BRONZE_IMPORT_STALE_LOCK_REQUIRES_MANUAL_REVIEW: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise BronzeImportError(
                "STOP_BRONZE_IMPORT_STALE_LOCK_REQUIRES_MANUAL_REVIEW"
            )
        return value

    @staticmethod
    def pid_alive(pid: int) -> bool:
        try:
            os.kill(int(pid), 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    @classmethod
    def inspect_existing(cls, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        metadata = cls.read(path)
        pid = int(metadata.get("pid") or 0)
        if pid > 0 and cls.pid_alive(pid):
            raise BronzeImportError("STOP_BRONZE_IMPORT_ALREADY_RUNNING")
        raise BronzeImportError(
            "STOP_BRONZE_IMPORT_STALE_LOCK_REQUIRES_MANUAL_REVIEW"
        )

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {**self.metadata, "token": self.token}
        try:
            descriptor = os.open(
                self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
        except FileExistsError:
            self.inspect_existing(self.path)
            raise AssertionError("unreachable")
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.acquired = True

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            current = self.read(self.path)
            if current.get("token") == self.token:
                self.path.unlink()
        finally:
            self.acquired = False

    def __enter__(self) -> "ImportLock":
        self.acquire()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.release()


def _register_target_contract(
    client: Any, config: ImportConfig, segments: Sequence[SegmentSpec]
) -> str:
    existing = client.query(
        f"""
        SELECT {", ".join(SEGMENT_SELECT_COLUMNS)}
        FROM {config.database}.{SEGMENTS_TABLE} FINAL
        WHERE symbol = {{symbol:String}} AND chain_version = {{chain_version:String}}
        ORDER BY canonical_segment_chain_index
        """,
        parameters={"symbol": config.symbol, "chain_version": config.chain_version},
    ).result_rows
    if existing:
        actual = [
            SegmentSpec(
                **{
                    name: (
                        _text(value)
                        if name
                        in {
                            "symbol",
                            "chain_version",
                            "source_segment_sha256",
                            "source_path",
                            "archive_instance_id",
                            "resolution_status",
                            "predecessor_segment_sha256",
                            "anchor_type",
                            "anchor_provenance",
                            "continuity_status",
                            "canonical_chain_hash",
                        }
                        else int(value)
                    )
                    for name, value in zip(SEGMENT_SELECT_COLUMNS, row)
                }
            )
            for row in existing
        ]
        if actual != list(segments):
            raise BronzeImportError(
                "STOP_BRONZE_RUNNER_SAFETY_TARGET_CHAIN_MAPPING_MISMATCH"
            )
        return "SKIPPED_IDENTICAL_CHAIN"
    now = int(time.time() * 1000)
    columns = list(SEGMENT_SELECT_COLUMNS[:-1]) + ["canonical_chain_hash", "created_at_ms"]
    rows = []
    for segment in segments:
        values = asdict(segment)
        values["created_at_ms"] = now
        rows.append([values[column] for column in columns])
    client.insert(
        f"{config.database}.{SEGMENTS_TABLE}", rows, column_names=columns
    )
    return "INSERTED"


def preflight(
    client: Any,
    config: ImportConfig,
    *,
    verify_sha: bool = True,
    require_target_schema: bool = False,
    check_lock: bool = True,
    stop: StopState | None = None,
) -> tuple[list[SegmentSpec], dict[str, Any]]:
    validate_target_database(config.database)
    identity = code_identity()
    if identity["branch"] != EXPECTED_BRANCH or identity["head"] == "UNKNOWN":
        raise BronzeImportError("STOP_BRONZE_RUNNER_SAFETY_WRONG_BRANCH")
    if config.start_chain_index < 0 or config.end_chain_index < config.start_chain_index:
        raise BronzeImportError("STOP_BRONZE_RUNNER_SAFETY_CHAIN_RANGE_INVALID")
    if config.end_chain_index >= config.expected_segment_count:
        raise BronzeImportError("STOP_BRONZE_RUNNER_SAFETY_CHAIN_RANGE_INVALID")
    if config.progress_every_segments < 1:
        raise BronzeImportError("STOP_BRONZE_RUNNER_SAFETY_PROGRESS_INTERVAL_INVALID")
    if (
        config.enforce_canonical_lock_path
        and config.lock_path.resolve() != DEFAULT_LOCK_PATH.resolve()
    ):
        raise BronzeImportError(
            "STOP_BRONZE_RUNNER_SAFETY_NONCANONICAL_LOCK_PATH"
        )
    if stop is not None:
        stop.check()
    client.command("SELECT 1")
    client.command("SET max_threads = 1")
    segments = load_source_contract(client, config)
    try:
        verified = verify_archives(
            segments, config, verify_sha=verify_sha, stop=stop
        )
    except RuntimeError as exc:
        if isinstance(exc, BronzeImportError):
            raise
        raise BronzeImportError(
            f"STOP_BRONZE_RUNNER_SAFETY_ARCHIVE_VALIDATION: {exc}"
        ) from exc
    rss, free = _check_resources(client, config)
    schema_exists = target_schema_exists(client, config.database)
    if schema_exists:
        validate_target_schema(client, config.database)
    if require_target_schema and not schema_exists:
        raise BronzeImportError("STOP_BRONZE_RUNNER_SAFETY_TARGET_SCHEMA_MISSING")
    if check_lock:
        ImportLock.inspect_existing(config.lock_path)
    local_free = shutil.disk_usage(config.archive_root).free
    report = {
        "status": "PASS",
        "mode": "READ_ONLY_PREFLIGHT",
        "code": identity,
        "database": config.database,
        "symbol": config.symbol,
        "chain_version": config.chain_version,
        "canonical_chain_hash": config.expected_chain_hash,
        "segments": len(verified),
        "unique_segment_shas": len({row.source_segment_sha256 for row in verified}),
        "unique_chain_indices": len(
            {row.canonical_segment_chain_index for row in verified}
        ),
        "target_schema_exists": schema_exists,
        "clickhouse_free_bytes": free,
        "archive_filesystem_free_bytes": local_free,
        "rss_bytes": rss,
        "lock_status": "FREE",
    }
    return verified, report


def _install_signal_handlers(stop: StopState) -> dict[int, Any]:
    previous: dict[int, Any] = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, lambda received, _frame, s=stop: s.request(received))
    return previous


def _restore_signal_handlers(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def _progress(
    *,
    segment: SegmentSpec,
    position: int,
    total: int,
    segment_records: int,
    total_records: int,
    started: float,
    rss_bytes: int,
    free_bytes: int,
    status: str,
) -> dict[str, Any]:
    elapsed = max(time.monotonic() - started, 1e-9)
    rate = total_records / elapsed
    remaining_segments = max(total - position, 0)
    eta = elapsed / max(position, 1) * remaining_segments
    row = {
        "segment": f"{position}/{total}",
        "chain_index": segment.canonical_segment_chain_index,
        "utc_start": _ns_iso(segment.segment_start_ns),
        "utc_end": _ns_iso(segment.segment_end_ns),
        "segment_records": segment_records,
        "total_records": total_records,
        "elapsed_s": round(elapsed, 3),
        "records_per_s": round(rate, 3),
        "peak_rss_mib": round(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 3
        ),
        "current_rss_mib": round(rss_bytes / 1024**2, 3),
        "free_disk_gib": round(free_bytes / 1024**3, 3),
        "eta_s": round(eta, 3),
        "status": status,
    }
    print(json.dumps(row, sort_keys=True), flush=True)
    return row


def import_one_segment(
    client: Any,
    config: ImportConfig,
    segment: SegmentSpec,
    stop: StopState,
) -> tuple[int, int]:
    prior = _ledger_status(
        client, config.database, _segment_import_id(config, segment)
    )
    if prior == "COMPLETE":
        if _segment_is_complete(client, config, segment):
            return 0, segment.record_count
    existing = _target_segment_counts(client, config, segment)
    existing_count, unique_ids, unique_ordinals, invalid_ids, minimum, maximum = existing
    if existing_count and (
        unique_ids != existing_count
        or unique_ordinals != existing_count
        or invalid_ids
        or minimum != 1
        or maximum != existing_count
        or existing_count > segment.record_count
    ):
        raise BronzeImportError(
            "STOP_BRONZE_RUNNER_RESUME_PARTIAL_SEGMENT_NOT_CONTIGUOUS"
        )
    if (prior is not None or existing_count) and not config.resume:
        raise BronzeImportError(
            "STOP_BRONZE_RUNNER_RESUME_INCOMPLETE_REQUIRES_RESUME"
        )
    _write_ledger(
        client,
        config,
        segment,
        status="RESUMING" if prior else "RUNNING",
        records_seen=0,
        rows_inserted=0,
    )
    records_seen = rows_inserted = 0
    ingestion_ms = int(time.time() * 1000)
    batch: list[tuple[Any, ...]] = []
    record_ids: set[str] = set()
    last_ordinal = 0
    try:
        for record, ordinal in iter_records_stream(Path(segment.source_path)):
            stop.check()
            if ordinal <= last_ordinal:
                raise BronzeImportError(
                    "STOP_BRONZE_RUNNER_SAFETY_RECORD_ORDINAL_NOT_STRICT"
                )
            row = _event_row(
                record, ordinal, asdict(segment), config.chain_version, ingestion_ms
            )
            record_id = str(row[0])
            if record_id in record_ids:
                raise BronzeImportError(
                    "STOP_BRONZE_RUNNER_SAFETY_DUPLICATE_RECORD_ID"
                )
            record_ids.add(record_id)
            last_ordinal = ordinal
            records_seen += 1
            if ordinal <= existing_count:
                continue
            batch.append(row)
            if len(batch) >= BATCH_SIZE:
                client.insert(
                    f"{config.database}.{EVENTS_TABLE}",
                    batch,
                    column_names=EVENT_COLUMNS,
                )
                rows_inserted += len(batch)
                batch.clear()
                stop.check()
                _check_resources(client, config)
        if batch:
            client.insert(
                f"{config.database}.{EVENTS_TABLE}",
                batch,
                column_names=EVENT_COLUMNS,
            )
            rows_inserted += len(batch)
            batch.clear()
        stop.check()
        if records_seen != segment.record_count:
            raise BronzeImportError(
                "STOP_BRONZE_RUNNER_SAFETY_SEGMENT_RECORD_COUNT_MISMATCH"
            )
        counts = _target_segment_counts(client, config, segment)
        if counts != (
            segment.record_count,
            segment.record_count,
            segment.record_count,
            0,
            1,
            segment.record_count,
        ):
            raise BronzeImportError(
                "STOP_BRONZE_RUNNER_RESUME_SEGMENT_FINAL_COUNT_MISMATCH"
            )
        _write_ledger(
            client,
            config,
            segment,
            status="COMPLETE",
            records_seen=records_seen,
            rows_inserted=rows_inserted,
        )
        return rows_inserted, records_seen
    except BaseException as exc:
        _write_ledger(
            client,
            config,
            segment,
            status="INTERRUPTED",
            records_seen=records_seen,
            rows_inserted=rows_inserted,
            error_message=str(exc),
        )
        raise


def run_import(
    client: Any,
    config: ImportConfig,
    segments: Sequence[SegmentSpec],
    *,
    stop: StopState | None = None,
) -> dict[str, Any]:
    stop = stop or StopState()
    selected = [
        row
        for row in segments
        if config.start_chain_index
        <= row.canonical_segment_chain_index
        <= config.end_chain_index
    ]
    if not selected:
        raise BronzeImportError("STOP_BRONZE_RUNNER_SAFETY_EMPTY_CHAIN_RANGE")
    started = time.monotonic()
    total_records = 0
    progress: list[dict[str, Any]] = []
    skipped = completed = 0
    for position, segment in enumerate(selected, 1):
        stop.check()
        inserted, logical_records = import_one_segment(
            client, config, segment, stop
        )
        status = "SKIPPED_ALREADY_COMPLETE" if inserted == 0 else "COMPLETE"
        skipped += int(inserted == 0)
        completed += int(inserted > 0)
        total_records += logical_records
        rss, free = _check_resources(client, config)
        progress.append(
            _progress(
                segment=segment,
                position=position,
                total=len(selected),
                segment_records=logical_records,
                total_records=total_records,
                started=started,
                rss_bytes=rss,
                free_bytes=free,
                status=status,
            )
        )
        if (
            position % config.progress_every_segments == 0
            or position == len(selected)
        ):
            _write_report(
                config.report_path,
                {
                    "status": "RUNNING" if position < len(selected) else "COMPLETE",
                    "segments_processed": position,
                    "segments_total": len(selected),
                    "logical_records": total_records,
                    "last_progress": progress[-1],
                },
            )
    return {
        "status": "COMPLETE",
        "selected_segments": len(selected),
        "completed_segments": completed,
        "skipped_segments": skipped,
        "logical_records": total_records,
        "elapsed_s": round(time.monotonic() - started, 3),
        "progress": progress,
    }


def verify_target(
    client: Any, config: ImportConfig, segments: Sequence[SegmentSpec]
) -> dict[str, Any]:
    if not target_schema_exists(client, config.database):
        raise BronzeImportError("STOP_BRONZE_RUNNER_SAFETY_TARGET_SCHEMA_MISSING")
    target = client.query(
        f"""
        SELECT {", ".join(SEGMENT_SELECT_COLUMNS)}
        FROM {config.database}.{SEGMENTS_TABLE} FINAL
        WHERE symbol = {{symbol:String}} AND chain_version = {{chain_version:String}}
        ORDER BY canonical_segment_chain_index
        """,
        parameters={"symbol": config.symbol, "chain_version": config.chain_version},
    ).result_rows
    actual = [
        SegmentSpec(
            **{
                name: (
                    _text(value)
                    if name
                    in {
                        "symbol",
                        "chain_version",
                        "source_segment_sha256",
                        "source_path",
                        "archive_instance_id",
                        "resolution_status",
                        "predecessor_segment_sha256",
                        "anchor_type",
                        "anchor_provenance",
                        "continuity_status",
                        "canonical_chain_hash",
                    }
                    else int(value)
                )
                for name, value in zip(SEGMENT_SELECT_COLUMNS, row)
            }
        )
        for row in target
    ]
    if actual != list(segments):
        raise BronzeImportError(
            "STOP_BRONZE_RUNNER_SAFETY_TARGET_CHAIN_MAPPING_MISMATCH"
        )
    total = 0
    for segment in segments:
        if not _segment_is_complete(client, config, segment):
            raise BronzeImportError(
                "STOP_BRONZE_RUNNER_RESUME_SEGMENT_NOT_COMPLETE"
            )
        total += segment.record_count
    utc_bad = int(
        client.query(
            f"""
            SELECT count()
            FROM {config.database}.{EVENTS_TABLE} FINAL
            WHERE symbol = {{symbol:String}}
              AND chain_version = {{chain_version:String}}
              AND (
                toUnixTimestamp64Nano(event_time) != event_time_ns
                OR toUnixTimestamp64Nano(receive_time) != receive_time_ns
              )
            """,
            parameters={
                "symbol": config.symbol,
                "chain_version": config.chain_version,
            },
        ).result_rows[0][0]
    )
    if utc_bad:
        raise BronzeImportError("STOP_BRONZE_RUNNER_SAFETY_UTC_NS_MISMATCH")
    return {
        "status": "VERIFIED",
        "segments": len(segments),
        "records": total,
        "utc_ns_mismatches": utc_bad,
    }


def _lock_metadata(config: ImportConfig) -> dict[str, Any]:
    identity = code_identity()
    return {
        "pid": os.getpid(),
        "started_at": _now_iso(),
        "host": socket.gethostname(),
        "chain_version": config.chain_version,
        "chain_hash": config.expected_chain_hash,
        "database": config.database,
        "branch": identity["branch"],
        "head": identity["head"],
    }


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Guarded BTC Bronze v1.3 full-import runner"
    )
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--chain-version", required=True)
    parser.add_argument("--expected-chain-hash", required=True)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--init-schema", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-only", action="store_true")
    mode.add_argument("--run", action="store_true")
    mode.add_argument("--verify-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--start-chain-index", type=int, default=0)
    parser.add_argument("--end-chain-index", type=int, default=163)
    parser.add_argument("--max-rss-mib", type=int, default=DEFAULT_MAX_RSS_MIB)
    parser.add_argument(
        "--min-free-disk-gib", type=float, default=DEFAULT_MIN_FREE_DISK_GIB
    )
    parser.add_argument("--progress-every-segments", type=int, default=1)
    parser.add_argument("--report-path", type=Path, required=True)
    parser.add_argument("--lock-path", type=Path, required=True)
    return parser


def config_from_args(args: argparse.Namespace) -> ImportConfig:
    return ImportConfig(
        symbol=str(args.symbol).upper(),
        chain_version=str(args.chain_version),
        expected_chain_hash=_sha_text(
            str(args.expected_chain_hash), label="CHAIN_HASH"
        ),
        archive_root=Path(args.archive_root),
        database=str(args.database),
        resume=bool(args.resume),
        start_chain_index=int(args.start_chain_index),
        end_chain_index=int(args.end_chain_index),
        max_rss_mib=int(args.max_rss_mib),
        min_free_disk_gib=float(args.min_free_disk_gib),
        progress_every_segments=int(args.progress_every_segments),
        report_path=Path(args.report_path),
        lock_path=Path(args.lock_path),
    )


def execute(
    args: argparse.Namespace, *, client: Any | None = None
) -> dict[str, Any]:
    if not (args.check_only or args.run or args.verify_only or args.init_schema):
        raise BronzeImportError("STOP_BRONZE_RUNNER_SAFETY_EXPLICIT_MODE_REQUIRED")
    if args.resume and not args.run:
        raise BronzeImportError("STOP_BRONZE_RUNNER_RESUME_REQUIRES_RUN")
    if (args.check_only or args.verify_only) and args.init_schema:
        raise BronzeImportError("STOP_BRONZE_RUNNER_SAFETY_READ_ONLY_MODE_WITH_DDL")
    config = config_from_args(args)
    own_client = client is None
    client = client or get_clickhouse_client()
    try:
        if args.check_only:
            _, report = preflight(client, config, require_target_schema=False)
            print(json.dumps(report, indent=2, sort_keys=True), flush=True)
            return report
        if args.verify_only:
            segments, preflight_report = preflight(
                client, config, require_target_schema=True
            )
            result = verify_target(client, config, segments)
            payload = {"preflight": preflight_report, "verify": result}
            print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
            return payload

        stop = StopState()
        previous = _install_signal_handlers(stop)
        try:
            lock = ImportLock(config.lock_path, _lock_metadata(config))
            with lock:
                try:
                    segments, preflight_report = preflight(
                        client,
                        config,
                        require_target_schema=not args.init_schema,
                        check_lock=False,
                        stop=stop,
                    )
                    if args.init_schema:
                        init_schema(client, config.database)
                        validate_target_schema(client, config.database)
                    if not args.run:
                        payload = {
                            "status": "SCHEMA_INITIALIZED",
                            "registration": "NOT_REQUESTED_SCHEMA_ONLY",
                            "preflight": preflight_report,
                        }
                        _write_report(config.report_path, payload)
                        print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
                        return payload
                    if not target_schema_exists(client, config.database):
                        raise BronzeImportError(
                            "STOP_BRONZE_RUNNER_SAFETY_TARGET_SCHEMA_MISSING"
                        )
                    validate_target_schema(client, config.database)
                    _register_target_contract(client, config, segments)
                    _write_report(
                        config.report_path,
                        {
                            "status": "STARTING",
                            "preflight": preflight_report,
                            "started_at": _now_iso(),
                        },
                    )
                    result = run_import(client, config, segments, stop=stop)
                    payload = {
                        "verdict": "BTC_BRONZE_V1_3_FULL_IMPORT_COMPLETE",
                        "preflight": preflight_report,
                        "result": result,
                    }
                    _write_report(config.report_path, payload)
                    return payload
                except BaseException as exc:
                    interrupted = isinstance(exc, ControlledInterrupt)
                    _write_report(
                        config.report_path,
                        {
                            "status": "INTERRUPTED" if interrupted else "FAILED",
                            "error": str(exc),
                            "signal": stop.signal_number,
                            "failed_at": _now_iso(),
                        },
                    )
                    if isinstance(exc, BronzeImportError):
                        raise
                    raise BronzeImportError(
                        f"STOP_BRONZE_RUNNER_SAFETY_UNEXPECTED: {exc}"
                    ) from exc
        finally:
            _restore_signal_handlers(previous)
    finally:
        if own_client:
            client.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = execute(args)
        if args.run:
            print(json.dumps(result, indent=2, sort_keys=True), flush=True)
        return 0
    except BronzeImportError as exc:
        print(
            json.dumps(
                {"verdict": str(exc).split(":", 1)[0], "error": str(exc)},
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )
        return 2
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "verdict": "STOP_BRONZE_RUNNER_SAFETY_UNEXPECTED",
                    "error": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""DDL and schema introspection for Full-OB continuous pilot v1_2 (UTC/ns-safe)."""

from __future__ import annotations

from typing import Any

from . import DATABASE, EVENTS_TABLE, LEDGER_TABLE, SCHEMA_VERSION

EVENTS_DDL = f"""
CREATE TABLE IF NOT EXISTS {DATABASE}.{EVENTS_TABLE}
(
    record_id FixedString(64),
    symbol LowCardinality(String),
    event_time DateTime64(9, 'UTC'),
    event_time_ns UInt64,
    receive_time DateTime64(9, 'UTC'),
    receive_time_ns UInt64,
    message_type LowCardinality(String),
    update_id UInt64,
    seq UInt64,
    update_id_present UInt8,
    seq_present UInt8,
    event_time_present UInt8,
    receive_time_present UInt8,
    source_segment String,
    source_segment_sha256 FixedString(64),
    record_ordinal UInt64,
    payload_sha256 FixedString(64),
    original_payload String,
    envelope_version String,
    ingestion_ts DateTime64(3, 'UTC'),
    ingestion_ts_ms UInt64
)
ENGINE = ReplacingMergeTree(ingestion_ts)
PARTITION BY (symbol, toYYYYMMDD(event_time))
ORDER BY (symbol, event_time_ns, update_id, seq, record_id)
""".strip()

LEDGER_DDL = f"""
CREATE TABLE IF NOT EXISTS {DATABASE}.{LEDGER_TABLE}
(
    import_id FixedString(64),
    schema_version LowCardinality(String),
    symbol LowCardinality(String),
    window_start DateTime64(9, 'UTC'),
    window_start_ns UInt64,
    window_end DateTime64(9, 'UTC'),
    window_end_ns UInt64,
    source_segment String,
    source_segment_sha256 FixedString(64),
    status LowCardinality(String),
    rows_seen UInt64,
    rows_in_window UInt64,
    rows_inserted UInt64,
    min_event_time Nullable(DateTime64(9, 'UTC')),
    min_event_time_ns Nullable(UInt64),
    max_event_time Nullable(DateTime64(9, 'UTC')),
    max_event_time_ns Nullable(UInt64),
    started_at DateTime64(3, 'UTC'),
    started_at_ms UInt64,
    completed_at Nullable(DateTime64(3, 'UTC')),
    completed_at_ms Nullable(UInt64),
    error_message String,
    ledger_version UInt64
)
ENGINE = ReplacingMergeTree(ledger_version)
ORDER BY (import_id)
""".strip()

DATABASE_DDL = f"CREATE DATABASE IF NOT EXISTS {DATABASE}"

EXPECTED_EVENTS_COLUMNS: dict[str, str] = {
    "record_id": "FixedString(64)",
    "symbol": "LowCardinality(String)",
    "event_time": "DateTime64(9, 'UTC')",
    "event_time_ns": "UInt64",
    "receive_time": "DateTime64(9, 'UTC')",
    "receive_time_ns": "UInt64",
    "message_type": "LowCardinality(String)",
    "update_id": "UInt64",
    "seq": "UInt64",
    "update_id_present": "UInt8",
    "seq_present": "UInt8",
    "event_time_present": "UInt8",
    "receive_time_present": "UInt8",
    "source_segment": "String",
    "source_segment_sha256": "FixedString(64)",
    "record_ordinal": "UInt64",
    "payload_sha256": "FixedString(64)",
    "original_payload": "String",
    "envelope_version": "String",
    "ingestion_ts": "DateTime64(3, 'UTC')",
    "ingestion_ts_ms": "UInt64",
}

EXPECTED_LEDGER_COLUMNS: dict[str, str] = {
    "import_id": "FixedString(64)",
    "schema_version": "LowCardinality(String)",
    "symbol": "LowCardinality(String)",
    "window_start": "DateTime64(9, 'UTC')",
    "window_start_ns": "UInt64",
    "window_end": "DateTime64(9, 'UTC')",
    "window_end_ns": "UInt64",
    "source_segment": "String",
    "source_segment_sha256": "FixedString(64)",
    "status": "LowCardinality(String)",
    "rows_seen": "UInt64",
    "rows_in_window": "UInt64",
    "rows_inserted": "UInt64",
    "min_event_time": "Nullable(DateTime64(9, 'UTC'))",
    "min_event_time_ns": "Nullable(UInt64)",
    "max_event_time": "Nullable(DateTime64(9, 'UTC'))",
    "max_event_time_ns": "Nullable(UInt64)",
    "started_at": "DateTime64(3, 'UTC')",
    "started_at_ms": "UInt64",
    "completed_at": "Nullable(DateTime64(3, 'UTC'))",
    "completed_at_ms": "Nullable(UInt64)",
    "error_message": "String",
    "ledger_version": "UInt64",
}


def ensure_database_and_tables(client: Any) -> None:
    client.command(DATABASE_DDL)
    client.command(EVENTS_DDL)
    client.command(LEDGER_DDL)


def _normalize_type(type_name: str) -> str:
    return type_name.replace(" ", "")


def assert_compatible_schema(client: Any) -> None:
    """Raise RuntimeError with STOP_PILOT_SCHEMA_CONFLICT semantics if schema mismatches."""
    for table, expected in (
        (EVENTS_TABLE, EXPECTED_EVENTS_COLUMNS),
        (LEDGER_TABLE, EXPECTED_LEDGER_COLUMNS),
    ):
        rows = client.query(
            "SELECT name, type FROM system.columns "
            f"WHERE database = '{DATABASE}' AND table = '{table}' "
            "ORDER BY position"
        ).result_rows
        if not rows:
            raise RuntimeError(f"STOP_PILOT_SCHEMA_CONFLICT: missing table {DATABASE}.{table}")
        actual = {str(name): _normalize_type(str(typ)) for name, typ in rows}
        exp_norm = {k: _normalize_type(v) for k, v in expected.items()}
        if set(actual) != set(exp_norm):
            missing = sorted(set(exp_norm) - set(actual))
            extra = sorted(set(actual) - set(exp_norm))
            raise RuntimeError(
                f"STOP_PILOT_SCHEMA_CONFLICT: column set mismatch on {table}: "
                f"missing={missing} extra={extra}"
            )
        for col, want in exp_norm.items():
            got = actual[col]
            if got != want:
                raise RuntimeError(
                    f"STOP_PILOT_SCHEMA_CONFLICT: {table}.{col} type {got!r} != {want!r}"
                )


def schema_version() -> str:
    return SCHEMA_VERSION

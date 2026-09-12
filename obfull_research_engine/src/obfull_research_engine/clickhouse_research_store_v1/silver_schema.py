"""DDL for Silver pilot tables v1_2 (create-if-not-exists + compatibility check)."""

from __future__ import annotations

from typing import Any

from . import DATABASE
from .silver_constants import (
    BUILDS_TABLE,
    CHECKPOINTS_TABLE,
    LEVEL_CHANGES_TABLE,
    METRICS_TABLE,
)

CHECKPOINTS_DDL = f"""
CREATE TABLE IF NOT EXISTS {DATABASE}.{CHECKPOINTS_TABLE}
(
    symbol LowCardinality(String),
    checkpoint_time DateTime64(9, 'UTC'),
    checkpoint_time_ns UInt64,
    replay_epoch Int64,
    source_record_id FixedString(64),
    source_record_ordinal UInt64,
    source_segment_sha256 FixedString(64),
    update_id UInt64,
    seq UInt64,
    bid_level_count UInt32,
    ask_level_count UInt32,
    best_bid Float64,
    best_ask Float64,
    book_hash FixedString(64),
    checkpoint_payload String,
    silver_build_id FixedString(64),
    created_at DateTime64(3, 'UTC'),
    created_at_ms UInt64
)
ENGINE = ReplacingMergeTree(created_at)
PARTITION BY (symbol, toYYYYMMDD(checkpoint_time))
ORDER BY (symbol, checkpoint_time_ns, source_record_id, silver_build_id)
""".strip()

LEVEL_CHANGES_DDL = f"""
CREATE TABLE IF NOT EXISTS {DATABASE}.{LEVEL_CHANGES_TABLE}
(
    symbol LowCardinality(String),
    event_time DateTime64(9, 'UTC'),
    event_time_ns UInt64,
    receive_time_ns UInt64,
    replay_epoch Int64,
    apply_order UInt64,
    message_order UInt64,
    level_order UInt32,
    side LowCardinality(String),
    price Float64,
    old_size Float64,
    new_size Float64,
    change_type LowCardinality(String),
    update_id UInt64,
    seq UInt64,
    source_record_id FixedString(64),
    source_record_ordinal UInt64,
    silver_build_id FixedString(64),
    created_at DateTime64(3, 'UTC'),
    created_at_ms UInt64
)
ENGINE = ReplacingMergeTree(created_at)
PARTITION BY (symbol, toYYYYMMDD(event_time))
ORDER BY (symbol, event_time_ns, apply_order, side, price, silver_build_id)
""".strip()

METRICS_DDL = f"""
CREATE TABLE IF NOT EXISTS {DATABASE}.{METRICS_TABLE}
(
    symbol LowCardinality(String),
    bucket_start DateTime64(3, 'UTC'),
    bucket_start_ms UInt64,
    replay_epoch Int64,
    best_bid Nullable(Float64),
    best_ask Nullable(Float64),
    mid Nullable(Float64),
    spread Nullable(Float64),
    bid_level_count UInt32,
    ask_level_count UInt32,
    bid_depth_near Float64,
    ask_depth_near Float64,
    last_update_id Nullable(UInt64),
    last_seq Nullable(UInt64),
    book_hash FixedString(64),
    silver_build_id FixedString(64),
    created_at DateTime64(3, 'UTC'),
    created_at_ms UInt64
)
ENGINE = ReplacingMergeTree(created_at)
PARTITION BY (symbol, toYYYYMMDD(bucket_start))
ORDER BY (symbol, bucket_start_ms, silver_build_id)
""".strip()

BUILDS_DDL = f"""
CREATE TABLE IF NOT EXISTS {DATABASE}.{BUILDS_TABLE}
(
    silver_build_id FixedString(64),
    schema_version LowCardinality(String),
    bronze_import_id FixedString(64),
    symbol LowCardinality(String),
    requested_start DateTime64(9, 'UTC'),
    requested_start_ns UInt64,
    requested_end DateTime64(9, 'UTC'),
    requested_end_ns UInt64,
    replay_coverage_start Nullable(DateTime64(9, 'UTC')),
    replay_coverage_start_ns Nullable(UInt64),
    replay_coverage_end Nullable(DateTime64(9, 'UTC')),
    replay_coverage_end_ns Nullable(UInt64),
    anchor_record_id String,
    anchor_time Nullable(DateTime64(9, 'UTC')),
    anchor_time_ns Nullable(UInt64),
    source_record_count UInt64,
    unique_record_count UInt64,
    delta_message_count UInt64,
    level_change_count UInt64,
    metric_bucket_count UInt64,
    replay_epoch_count UInt64,
    gap_count UInt64,
    reset_count UInt64,
    status LowCardinality(String),
    started_at DateTime64(3, 'UTC'),
    started_at_ms UInt64,
    completed_at Nullable(DateTime64(3, 'UTC')),
    completed_at_ms Nullable(UInt64),
    error_message String,
    contract_hash FixedString(64),
    build_version UInt64
)
ENGINE = ReplacingMergeTree(build_version)
ORDER BY (silver_build_id)
""".strip()

_EXPECTED: dict[str, dict[str, str]] = {
    CHECKPOINTS_TABLE: {
        "symbol": "LowCardinality(String)",
        "checkpoint_time": "DateTime64(9, 'UTC')",
        "checkpoint_time_ns": "UInt64",
        "replay_epoch": "Int64",
        "source_record_id": "FixedString(64)",
        "source_record_ordinal": "UInt64",
        "source_segment_sha256": "FixedString(64)",
        "update_id": "UInt64",
        "seq": "UInt64",
        "bid_level_count": "UInt32",
        "ask_level_count": "UInt32",
        "best_bid": "Float64",
        "best_ask": "Float64",
        "book_hash": "FixedString(64)",
        "checkpoint_payload": "String",
        "silver_build_id": "FixedString(64)",
        "created_at": "DateTime64(3, 'UTC')",
        "created_at_ms": "UInt64",
    },
    LEVEL_CHANGES_TABLE: {
        "symbol": "LowCardinality(String)",
        "event_time": "DateTime64(9, 'UTC')",
        "event_time_ns": "UInt64",
        "receive_time_ns": "UInt64",
        "replay_epoch": "Int64",
        "apply_order": "UInt64",
        "message_order": "UInt64",
        "level_order": "UInt32",
        "side": "LowCardinality(String)",
        "price": "Float64",
        "old_size": "Float64",
        "new_size": "Float64",
        "change_type": "LowCardinality(String)",
        "update_id": "UInt64",
        "seq": "UInt64",
        "source_record_id": "FixedString(64)",
        "source_record_ordinal": "UInt64",
        "silver_build_id": "FixedString(64)",
        "created_at": "DateTime64(3, 'UTC')",
        "created_at_ms": "UInt64",
    },
    METRICS_TABLE: {
        "symbol": "LowCardinality(String)",
        "bucket_start": "DateTime64(3, 'UTC')",
        "bucket_start_ms": "UInt64",
        "replay_epoch": "Int64",
        "best_bid": "Nullable(Float64)",
        "best_ask": "Nullable(Float64)",
        "mid": "Nullable(Float64)",
        "spread": "Nullable(Float64)",
        "bid_level_count": "UInt32",
        "ask_level_count": "UInt32",
        "bid_depth_near": "Float64",
        "ask_depth_near": "Float64",
        "last_update_id": "Nullable(UInt64)",
        "last_seq": "Nullable(UInt64)",
        "book_hash": "FixedString(64)",
        "silver_build_id": "FixedString(64)",
        "created_at": "DateTime64(3, 'UTC')",
        "created_at_ms": "UInt64",
    },
    BUILDS_TABLE: {
        "silver_build_id": "FixedString(64)",
        "schema_version": "LowCardinality(String)",
        "bronze_import_id": "FixedString(64)",
        "symbol": "LowCardinality(String)",
        "requested_start": "DateTime64(9, 'UTC')",
        "requested_start_ns": "UInt64",
        "requested_end": "DateTime64(9, 'UTC')",
        "requested_end_ns": "UInt64",
        "replay_coverage_start": "Nullable(DateTime64(9, 'UTC'))",
        "replay_coverage_start_ns": "Nullable(UInt64)",
        "replay_coverage_end": "Nullable(DateTime64(9, 'UTC'))",
        "replay_coverage_end_ns": "Nullable(UInt64)",
        "anchor_record_id": "String",
        "anchor_time": "Nullable(DateTime64(9, 'UTC'))",
        "anchor_time_ns": "Nullable(UInt64)",
        "source_record_count": "UInt64",
        "unique_record_count": "UInt64",
        "delta_message_count": "UInt64",
        "level_change_count": "UInt64",
        "metric_bucket_count": "UInt64",
        "replay_epoch_count": "UInt64",
        "gap_count": "UInt64",
        "reset_count": "UInt64",
        "status": "LowCardinality(String)",
        "started_at": "DateTime64(3, 'UTC')",
        "started_at_ms": "UInt64",
        "completed_at": "Nullable(DateTime64(3, 'UTC'))",
        "completed_at_ms": "Nullable(UInt64)",
        "error_message": "String",
        "contract_hash": "FixedString(64)",
        "build_version": "UInt64",
    },
}


def ensure_silver_tables(client: Any) -> None:
    from .schema import DATABASE_DDL

    client.command(DATABASE_DDL)
    for ddl in (CHECKPOINTS_DDL, LEVEL_CHANGES_DDL, METRICS_DDL, BUILDS_DDL):
        client.command(ddl)


def assert_silver_schema_compatible(client: Any) -> None:
    for table, expected in _EXPECTED.items():
        rows = client.query(
            "SELECT name, type FROM system.columns "
            f"WHERE database = '{DATABASE}' AND table = '{table}' "
            "ORDER BY position"
        ).result_rows
        if not rows:
            raise RuntimeError(f"STOP_SILVER_SCHEMA_CONFLICT: missing {DATABASE}.{table}")
        actual = {str(n): str(t).replace(" ", "") for n, t in rows}
        exp = {k: v.replace(" ", "") for k, v in expected.items()}
        if set(actual) != set(exp):
            raise RuntimeError(
                f"STOP_SILVER_SCHEMA_CONFLICT: {table} columns "
                f"missing={sorted(set(exp)-set(actual))} extra={sorted(set(actual)-set(exp))}"
            )
        for col, want in exp.items():
            if actual[col] != want:
                raise RuntimeError(
                    f"STOP_SILVER_SCHEMA_CONFLICT: {table}.{col} type {actual[col]!r} != {want!r}"
                )

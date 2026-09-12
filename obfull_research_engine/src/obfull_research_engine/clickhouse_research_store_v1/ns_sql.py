"""Canonical ClickHouse DateTime64 materialization from integer nanoseconds (UTC).

Never pass naive Python datetime/strings through clickhouse_connect: the client
interprets naive values in the host local timezone (Europe/Paris → −7200s).

Large payloads use a ns-only staging table + INSERT…SELECT fromUnixTimestamp64Nano.
"""

from __future__ import annotations

from typing import Any, Sequence


def sql_quote_string(value: str) -> str:
    """ClickHouse single-quoted string literal (backslash + quote escape)."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def from_unix_ns_utc_sql(ns: int) -> str:
    """SQL expression: fromUnixTimestamp64Nano(<ns>, 'UTC') — no float path."""
    return f"fromUnixTimestamp64Nano({int(ns)}, 'UTC')"


def from_unix_ms_utc_sql(ms: int) -> str:
    return f"fromUnixTimestamp64Milli({int(ms)}, 'UTC')"


def ensure_events_staging(client: Any, *, database: str, staging_table: str) -> None:
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {database}.{staging_table}
        (
            record_id FixedString(64),
            symbol LowCardinality(String),
            event_time_ns UInt64,
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
            ingestion_ts_ms UInt64
        )
        ENGINE = Memory
        """.strip()
    )


def insert_events_via_ns_staging(
    client: Any,
    *,
    database: str,
    events_table: str,
    staging_table: str,
    column_names: Sequence[str],
    rows: Sequence[Sequence[Any]],
) -> None:
    """Insert bronze event rows with DateTime64 derived only from *_ns / *_ms."""
    if not rows:
        return
    ensure_events_staging(client, database=database, staging_table=staging_table)
    # Staging columns (no DateTime64).
    staging_cols = [
        "record_id",
        "symbol",
        "event_time_ns",
        "receive_time_ns",
        "message_type",
        "update_id",
        "seq",
        "update_id_present",
        "seq_present",
        "event_time_present",
        "receive_time_present",
        "source_segment",
        "source_segment_sha256",
        "record_ordinal",
        "payload_sha256",
        "original_payload",
        "envelope_version",
        "ingestion_ts_ms",
    ]
    name_to_idx = {n: i for i, n in enumerate(column_names)}
    staging_rows = []
    for row in rows:
        staging_rows.append([row[name_to_idx[c]] for c in staging_cols])

    client.command(f"TRUNCATE TABLE {database}.{staging_table}")
    client.insert(
        f"{database}.{staging_table}",
        staging_rows,
        column_names=staging_cols,
    )
    client.command(
        f"""
        INSERT INTO {database}.{events_table}
        (
            record_id, symbol, event_time, event_time_ns, receive_time, receive_time_ns,
            message_type, update_id, seq, update_id_present, seq_present,
            event_time_present, receive_time_present, source_segment, source_segment_sha256,
            record_ordinal, payload_sha256, original_payload, envelope_version,
            ingestion_ts, ingestion_ts_ms
        )
        SELECT
            record_id,
            symbol,
            fromUnixTimestamp64Nano(event_time_ns, 'UTC') AS event_time,
            event_time_ns,
            fromUnixTimestamp64Nano(receive_time_ns, 'UTC') AS receive_time,
            receive_time_ns,
            message_type,
            update_id,
            seq,
            update_id_present,
            seq_present,
            event_time_present,
            receive_time_present,
            source_segment,
            source_segment_sha256,
            record_ordinal,
            payload_sha256,
            original_payload,
            envelope_version,
            fromUnixTimestamp64Milli(ingestion_ts_ms, 'UTC') AS ingestion_ts,
            ingestion_ts_ms
        FROM {database}.{staging_table}
        """.strip()
    )
    client.command(f"TRUNCATE TABLE {database}.{staging_table}")


def insert_checkpoints_via_ns_staging(
    client: Any,
    *,
    database: str,
    checkpoints_table: str,
    staging_table: str,
    rows: Sequence[dict[str, Any]],
) -> None:
    if not rows:
        return
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {database}.{staging_table}
        (
            symbol LowCardinality(String),
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
            created_at_ms UInt64
        )
        ENGINE = Memory
        """.strip()
    )
    cols = [
        "symbol",
        "checkpoint_time_ns",
        "replay_epoch",
        "source_record_id",
        "source_record_ordinal",
        "source_segment_sha256",
        "update_id",
        "seq",
        "bid_level_count",
        "ask_level_count",
        "best_bid",
        "best_ask",
        "book_hash",
        "checkpoint_payload",
        "silver_build_id",
        "created_at_ms",
    ]
    client.command(f"TRUNCATE TABLE {database}.{staging_table}")
    client.insert(
        f"{database}.{staging_table}",
        [[r[c] for c in cols] for r in rows],
        column_names=cols,
    )
    client.command(
        f"""
        INSERT INTO {database}.{checkpoints_table}
        (
            symbol, checkpoint_time, checkpoint_time_ns, replay_epoch,
            source_record_id, source_record_ordinal, source_segment_sha256,
            update_id, seq, bid_level_count, ask_level_count, best_bid, best_ask,
            book_hash, checkpoint_payload, silver_build_id, created_at, created_at_ms
        )
        SELECT
            symbol,
            fromUnixTimestamp64Nano(checkpoint_time_ns, 'UTC'),
            checkpoint_time_ns,
            replay_epoch,
            source_record_id,
            source_record_ordinal,
            source_segment_sha256,
            update_id,
            seq,
            bid_level_count,
            ask_level_count,
            best_bid,
            best_ask,
            book_hash,
            checkpoint_payload,
            silver_build_id,
            fromUnixTimestamp64Milli(created_at_ms, 'UTC'),
            created_at_ms
        FROM {database}.{staging_table}
        """.strip()
    )
    client.command(f"TRUNCATE TABLE {database}.{staging_table}")


def insert_rows_with_ns_datetimes(
    client: Any,
    *,
    database: str,
    table: str,
    column_names: Sequence[str],
    rows: Sequence[Sequence[Any]],
    datetime_ns_columns: dict[str, str],
    datetime_ms_columns: dict[str, str] | None = None,
) -> None:
    """INSERT small/medium rows where DateTime64 columns come from sibling ns/ms ints."""
    if not rows:
        return
    datetime_ms_columns = datetime_ms_columns or {}
    name_to_idx = {name: i for i, name in enumerate(column_names)}

    value_sql_parts: list[str] = []
    for row in rows:
        if len(row) != len(column_names):
            raise ValueError("row width mismatch")
        cells: list[str] = []
        for col, value in zip(column_names, row):
            if col in datetime_ns_columns:
                ns_val = row[name_to_idx[datetime_ns_columns[col]]]
                cells.append(from_unix_ns_utc_sql(int(ns_val)))
            elif col in datetime_ms_columns:
                ms_val = row[name_to_idx[datetime_ms_columns[col]]]
                cells.append(from_unix_ms_utc_sql(int(ms_val)))
            else:
                if value is None:
                    cells.append("NULL")
                elif isinstance(value, bool):
                    cells.append("1" if value else "0")
                elif isinstance(value, int):
                    cells.append(str(int(value)))
                elif isinstance(value, float):
                    cells.append(repr(float(value)))
                else:
                    cells.append(sql_quote_string(str(value)))
        value_sql_parts.append("(" + ", ".join(cells) + ")")

    cols_sql = ", ".join(column_names)
    sql = (
        f"INSERT INTO {database}.{table} ({cols_sql}) VALUES "
        + ", ".join(value_sql_parts)
    )
    client.command(sql)

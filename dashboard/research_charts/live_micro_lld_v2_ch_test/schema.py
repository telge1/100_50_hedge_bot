"""Dedicated ClickHouse tables — research test only, no production scanner tables."""

from __future__ import annotations

import clickhouse_connect

from ..clickhouse_config import load_clickhouse_config

TABLE_RUN = "research_lmv2_ch_test_run_v1"
TABLE_POOL = "research_lmv2_ch_test_pool_v1"

DDL_RUN = f"""
CREATE TABLE IF NOT EXISTS {{db}}.{TABLE_RUN} (
    run_id UUID,
    symbol String,
    timeframe String,
    pool_detail UInt8,
    engine_label String,
    candle_load_start DateTime64(3, 'UTC'),
    candle_first_open DateTime64(3, 'UTC'),
    candle_last_open DateTime64(3, 'UTC'),
    decision_close DateTime64(3, 'UTC'),
    candle_count UInt32,
    pool_count UInt32,
    data_complete UInt8,
    config_hash String,
    created_at DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = MergeTree()
ORDER BY (symbol, timeframe, pool_detail, decision_close, run_id)
"""

DDL_POOL = f"""
CREATE TABLE IF NOT EXISTS {{db}}.{TABLE_POOL} (
    run_id UUID,
    symbol String,
    timeframe String,
    pool_id String,
    pool_type LowCardinality(String),
    side LowCardinality(String),
    source_timestamp DateTime64(3, 'UTC'),
    created_timestamp DateTime64(3, 'UTC'),
    known_at DateTime64(3, 'UTC'),
    top_price Float64,
    bottom_price Float64,
    strength Float64,
    strength_class LowCardinality(String),
    active UInt8,
    break_at Nullable(DateTime64(3, 'UTC')),
    pivot_timestamp Nullable(DateTime64(3, 'UTC')),
    early_timing Nullable(Int32),
    reclaim_level Nullable(Float64),
    candidate_created_at Nullable(DateTime64(3, 'UTC')),
    confirmed_at Nullable(DateTime64(3, 'UTC')),
    invalidated_at Nullable(DateTime64(3, 'UTC')),
    created_index Int32,
    metadata_json String,
    inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(inserted_at)
ORDER BY (run_id, pool_id)
"""

# Non-destructive column adds (backward compatible with Phase-1 PD10 rows).
RUN_ALTER_COLUMNS = [
    "ADD COLUMN IF NOT EXISTS from_close DateTime64(3, 'UTC') DEFAULT toDateTime64('1970-01-01 00:00:00', 3, 'UTC')",
    "ADD COLUMN IF NOT EXISTS to_close DateTime64(3, 'UTC') DEFAULT toDateTime64('1970-01-01 00:00:00', 3, 'UTC')",
    "ADD COLUMN IF NOT EXISTS engine_version String DEFAULT ''",
    "ADD COLUMN IF NOT EXISTS engine_input_hash String DEFAULT ''",
    "ADD COLUMN IF NOT EXISTS candle_fingerprint String DEFAULT ''",
    "ADD COLUMN IF NOT EXISTS config_json String DEFAULT ''",
]


def ensure_test_tables() -> str:
    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    try:
        client.command(DDL_RUN.format(db=cfg.database))
        client.command(DDL_POOL.format(db=cfg.database))
        for alter in RUN_ALTER_COLUMNS:
            client.command(f"ALTER TABLE {cfg.database}.{TABLE_RUN} {alter}")
    finally:
        client.close()
    return cfg.database

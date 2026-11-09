"""Isolated ClickHouse tables for historical ACTIVE-state snapshots (Phase 5 research only)."""

from __future__ import annotations

import clickhouse_connect

from ...clickhouse_config import load_clickhouse_config

TABLE_SNAPSHOT_META = "research_lmv2_active_snapshot_meta_v1"
TABLE_SNAPSHOT_MEMBER = "research_lmv2_active_snapshot_member_v1"

DDL_META = f"""
CREATE TABLE IF NOT EXISTS {{db}}.{TABLE_SNAPSHOT_META} (
    snapshot_id String,
    test_run_id UUID,
    symbol String,
    timeframe String,
    pool_detail UInt8,
    config_hash String,
    engine_version String,
    engine_input_hash String,
    candle_load_start DateTime64(3, 'UTC'),
    history_from_close DateTime64(3, 'UTC'),
    decision_close DateTime64(3, 'UTC'),
    prefix_candle_count UInt32,
    prefix_candle_fingerprint String,
    known_pool_count UInt32,
    active_pool_count UInt32,
    active_upper_count UInt32,
    active_lower_count UInt32,
    active_state_fingerprint String,
    write_complete UInt8,
    created_at DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(created_at)
ORDER BY (symbol, timeframe, pool_detail, config_hash, decision_close, snapshot_id)
"""

DDL_MEMBER = f"""
CREATE TABLE IF NOT EXISTS {{db}}.{TABLE_SNAPSHOT_MEMBER} (
    snapshot_id String,
    pool_id String,
    side LowCardinality(String),
    sort_index UInt32,
    inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = MergeTree()
ORDER BY (snapshot_id, sort_index, pool_id)
"""


def ensure_active_snapshot_tables() -> str:
    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    try:
        client.command(DDL_META.format(db=cfg.database))
        client.command(DDL_MEMBER.format(db=cfg.database))
    finally:
        client.close()
    return cfg.database

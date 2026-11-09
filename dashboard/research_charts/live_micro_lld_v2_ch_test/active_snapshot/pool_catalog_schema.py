"""Active pool attribute rows per published snapshot (Phase 9 DOGE pilot)."""

from __future__ import annotations

import clickhouse_connect

from ...clickhouse_config import load_clickhouse_config

TABLE_ACTIVE_POOL = "research_lmv2_snapshot_active_pool_v1"

DDL = f"""
CREATE TABLE IF NOT EXISTS {{db}}.{TABLE_ACTIVE_POOL} (
    snapshot_id String,
    pipeline_line_id String,
    symbol String,
    timeframe String,
    pool_detail UInt8,
    decision_close DateTime64(3, 'UTC'),
    history_policy String,
    pool_id String,
    pool_type LowCardinality(String),
    side LowCardinality(String),
    top_price Float64,
    bottom_price Float64,
    strength Float64,
    strength_class LowCardinality(String),
    known_at DateTime64(3, 'UTC'),
    break_at Nullable(DateTime64(3, 'UTC')),
    reclaim_level Nullable(Float64),
    candidate_created_at Nullable(DateTime64(3, 'UTC')),
    confirmed_at Nullable(DateTime64(3, 'UTC')),
    invalidated_at Nullable(DateTime64(3, 'UTC')),
    created_index Int32,
    row_fingerprint String,
    inserted_at DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = MergeTree()
ORDER BY (snapshot_id, pool_id)
"""


def ensure_pool_catalog_table() -> str:
    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    try:
        client.command(DDL.format(db=cfg.database))
    finally:
        client.close()
    return cfg.database

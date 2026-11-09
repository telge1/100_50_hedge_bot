"""Publish ledger for validated ACTIVE snapshots (Phase 6 research only)."""

from __future__ import annotations

import clickhouse_connect

from ...clickhouse_config import load_clickhouse_config
from .schema import ensure_active_snapshot_tables

TABLE_PUBLISH = "research_lmv2_active_snapshot_publish_v1"

DDL_PUBLISH = f"""
CREATE TABLE IF NOT EXISTS {{db}}.{TABLE_PUBLISH} (
    publish_id String,
    pipeline_line_id String,
    snapshot_id String,
    symbol String,
    timeframe String,
    pool_detail UInt8,
    config_hash String,
    engine_version String,
    engine_input_hash String,
    candle_load_start DateTime64(3, 'UTC'),
    history_from_close DateTime64(3, 'UTC'),
    decision_close DateTime64(3, 'UTC'),
    prefix_candle_fingerprint String,
    active_state_fingerprint String,
    publish_complete UInt8,
    created_at DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(created_at)
ORDER BY (pipeline_line_id, symbol, timeframe, pool_detail, config_hash, decision_close, publish_id)
"""

PUBLISH_ALTER_COLUMNS = [
    "ADD COLUMN IF NOT EXISTS history_policy String DEFAULT ''",
]


def ensure_publish_tables() -> str:
    ensure_active_snapshot_tables()
    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    try:
        client.command(DDL_PUBLISH.format(db=cfg.database))
        for alter in PUBLISH_ALTER_COLUMNS:
            client.command(f"ALTER TABLE {cfg.database}.{TABLE_PUBLISH} {alter}")
    finally:
        client.close()
    return cfg.database

"""UTC-correct public-trade rematerialization contract (v2)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .contracts import ALLOWED_SYMBOLS, PROCESSOR_VERSION, TARGET_DATABASE, stable_hash, utc

TRADE_REMATERIALIZATION_CONTRACT_VERSION = "research_public_trades_contract_v2"
TRADE_REMATERIALIZATION_PROCESSOR = "btc_doge_research_trade_rematerialization_v1"
INVALID_SHIFTED_TABLE = "research_public_trades_invalid_shifted_v0"
WATERMARK_TABLE = "research_trade_rematerialization_watermarks"
RESULT_ROOT_NAME = "btc_doge_research_trade_rematerialization_v1"

# Planned research event coverage (UTC, exclusive end).
HISTORY_START = datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc)
HISTORY_END = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)

# Fight golden pilot window.
PILOT_SYMBOL = "BTCUSDT"
PILOT_START = datetime(2026, 8, 31, 18, 30, tzinfo=timezone.utc)
PILOT_END = datetime(2026, 8, 31, 19, 30, tzinfo=timezone.utc)

SHIFTED_BATCH_IDS = (
    "phase1:btc_run_018",
    "phase1:doge_20260829_1145_1230",
)

BUILD_ID = stable_hash(
    {
        "contract": TRADE_REMATERIALIZATION_CONTRACT_VERSION,
        "processor": TRADE_REMATERIALIZATION_PROCESSOR,
        "symbols": sorted(ALLOWED_SYMBOLS),
        "history_start": HISTORY_START.isoformat(),
        "history_end": HISTORY_END.isoformat(),
        "source": "orderbook_analysis.public_trades_canonical",
        "key": "(symbol, trade_id)",
    }
)

# Canonical logical key — proven unique in OA for planned range.
LOGICAL_KEY = ("symbol", "trade_id")

TRADE_V2_COLUMNS = (
    "symbol",
    "event_time",  # trade_ts_utc contract
    "receive_time",
    "trade_id",
    "price",
    "base_size",
    "quote_notional",
    "taker_side",
    "source",
    "source_id",
    "source_fingerprint",
    "source_segment_start",
    "source_segment_end",
    "source_contract_version",
    "processor_version",
    "ingestion_batch_id",
    "build_id",
    "contract_version",
    "record_version",
    "ingested_at",
    "imported_at",
    "quality_flags",
    "coverage_status",
    "finalization_status",
    "event_key",
)

WATERMARK_COLUMNS = (
    "build_id",
    "symbol",
    "segment_start",
    "segment_end",
    "status",
    "source_row_count",
    "source_unique_trade_ids",
    "rows_written",
    "source_fingerprint",
    "record_version",
    "started_at",
    "completed_at",
    "error",
)


def ensure_utc_aware(value: datetime) -> datetime:
    """Attach UTC to naive CH DateTime64 values; never reinterpret as local."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return utc(value)


def iso_z(value: datetime) -> str:
    return ensure_utc_aware(value).isoformat().replace("+00:00", "Z")


def literal_utc(value: datetime) -> str:
    """Format for toDateTime64(..., 'UTC') — no local conversion."""
    value = ensure_utc_aware(value)
    # ClickHouse parses 'YYYY-MM-DD HH:MM:SS.mmm' without timezone suffix.
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def segment_batch_id(symbol: str, segment_start: datetime) -> str:
    return f"trade_remat_v1:{symbol}:{iso_z(segment_start)}"


def source_segment_fingerprint(
    *,
    symbol: str,
    segment_start: datetime,
    segment_end: datetime,
    source_row_count: int,
    source_unique_trade_ids: int,
    min_trade_id: str,
    max_trade_id: str,
) -> str:
    return stable_hash(
        {
            "symbol": symbol,
            "start": iso_z(segment_start),
            "end": iso_z(segment_end),
            "source_row_count": source_row_count,
            "source_unique_trade_ids": source_unique_trade_ids,
            "min_trade_id": min_trade_id,
            "max_trade_id": max_trade_id,
            "contract": TRADE_REMATERIALIZATION_CONTRACT_VERSION,
        }
    )


DDL_WATERMARK = f"""
CREATE TABLE IF NOT EXISTS {TARGET_DATABASE}.{WATERMARK_TABLE}
(
    build_id String,
    symbol LowCardinality(String),
    segment_start DateTime64(3, 'UTC'),
    segment_end DateTime64(3, 'UTC'),
    status LowCardinality(String),
    source_row_count UInt64,
    source_unique_trade_ids UInt64,
    rows_written UInt64,
    source_fingerprint FixedString(64),
    record_version UInt64,
    started_at DateTime64(6, 'UTC'),
    completed_at Nullable(DateTime64(6, 'UTC')),
    error String DEFAULT ''
)
ENGINE = ReplacingMergeTree(record_version)
ORDER BY (build_id, symbol, segment_start)
"""

DDL_RESEARCH_PUBLIC_TRADES_V2 = f"""
CREATE TABLE IF NOT EXISTS {TARGET_DATABASE}.research_public_trades
(
    symbol LowCardinality(String),
    event_time DateTime64(3, 'UTC'),
    receive_time DateTime64(6, 'UTC'),
    trade_id String,
    price Decimal(18, 8),
    base_size Decimal(24, 12),
    quote_notional Decimal(24, 8),
    taker_side Enum8('Buy' = 1, 'Sell' = 2),
    source LowCardinality(String),
    source_id LowCardinality(String),
    source_fingerprint FixedString(64),
    source_segment_start DateTime64(3, 'UTC'),
    source_segment_end DateTime64(3, 'UTC'),
    source_contract_version LowCardinality(String),
    processor_version LowCardinality(String),
    ingestion_batch_id String,
    build_id String,
    contract_version LowCardinality(String),
    record_version UInt64,
    ingested_at DateTime64(6, 'UTC'),
    imported_at DateTime64(6, 'UTC'),
    quality_flags Array(LowCardinality(String)),
    coverage_status LowCardinality(String),
    finalization_status LowCardinality(String),
    event_key String
)
ENGINE = ReplacingMergeTree(record_version)
PARTITION BY (symbol, toYYYYMM(event_time))
ORDER BY (symbol, trade_id)
COMMENT 'research_public_trades_contract_v2; logical key (symbol, trade_id); event_time is trade_ts_utc'
"""


def contract_manifest() -> dict[str, Any]:
    return {
        "contract_version": TRADE_REMATERIALIZATION_CONTRACT_VERSION,
        "processor": TRADE_REMATERIALIZATION_PROCESSOR,
        "build_id": BUILD_ID,
        "logical_key": list(LOGICAL_KEY),
        "event_key": "{symbol}|{trade_id}",
        "time_contract": {
            "source_interpreted_as": "UTC",
            "column": "event_time DateTime64(3,'UTC')",
            "alias": "trade_ts_utc",
            "insert_path": "server-side INSERT SELECT (no Python naive datetime)",
            "forbidden": ["naive local reinterpretation", "blind +2h correction", "DST-local anchors"],
        },
        "engine": "ReplacingMergeTree(record_version)",
        "order_by": "(symbol, trade_id)",
        "history": {"start": iso_z(HISTORY_START), "end": iso_z(HISTORY_END)},
        "pilot": {"symbol": PILOT_SYMBOL, "start": iso_z(PILOT_START), "end": iso_z(PILOT_END)},
        "invalid_shifted_table": INVALID_SHIFTED_TABLE,
        "source_table": "orderbook_analysis.public_trades_canonical",
        "source_access": "READ_ONLY",
    }

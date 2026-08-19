"""ClickHouse writer for _v2 tables (manifest + features).

Uses INSERT ... VALUES via clickhouse_connect.  All Decimal fields are passed
as Python Decimal objects; all DateTime64 as Python datetime (UTC).
Idempotency is guaranteed by ReplacingMergeTree on the ORDER BY key:
re-inserting the same (exchange, market, symbol, depth, bucket_start) row
with a newer created_at will eventually replace the older one after OPTIMIZE or
during background merges. For immediate consistency we run OPTIMIZE FINAL on
small pilot tables after bulk insert.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

DB = "orderbook_analysis"
MANIFEST_TABLE = f"{DB}.orderbook_import_manifest_v2"
FEATURES_TABLE = f"{DB}.orderbook_features_1s_v2"

MANIFEST_COLS = [
    "exchange", "market", "symbol", "depth", "source_date", "source_url",
    "local_path", "sha256", "compressed_bytes", "raw_record_count",
    "source_min_ts", "source_max_ts", "downloaded_at", "import_started_at",
    "import_completed_at", "parser_version", "status", "error_message",
    "quality_flags", "inserted_feature_rows", "updated_at",
]

FEATURE_COLS = [
    "exchange", "market", "symbol", "depth", "bucket_start",
    "first_source_ts", "last_source_ts", "last_update_seq", "processed_updates",
    "parser_version", "created_at", "quality_flags", "is_valid",
    "best_bid_price", "best_bid_qty", "best_ask_price", "best_ask_qty",
    "mid_price", "microprice", "spread_abs", "spread_bps",
    "bid_qty_l5", "ask_qty_l5", "bid_notional_l5", "ask_notional_l5", "imbalance_l5",
    "bid_qty_l10", "ask_qty_l10", "bid_notional_l10", "ask_notional_l10", "imbalance_l10",
    "bid_qty_l25", "ask_qty_l25", "bid_notional_l25", "ask_notional_l25", "imbalance_l25",
    "bid_qty_l50", "ask_qty_l50", "bid_notional_l50", "ask_notional_l50", "imbalance_l50",
    "bid_qty_bps5", "ask_qty_bps5", "bid_notional_bps5", "ask_notional_bps5", "imbalance_bps5",
    "bid_qty_bps10", "ask_qty_bps10", "bid_notional_bps10", "ask_notional_bps10", "imbalance_bps10",
    "bid_qty_bps25", "ask_qty_bps25", "bid_notional_bps25", "ask_notional_bps25", "imbalance_bps25",
    "bid_qty_bps50", "ask_qty_bps50", "bid_notional_bps50", "ask_notional_bps50", "imbalance_bps50",
    "bid_wall_price", "bid_wall_qty", "bid_wall_notional", "bid_wall_bps_dist", "bid_wall_ratio",
    "ask_wall_price", "ask_wall_qty", "ask_wall_notional", "ask_wall_bps_dist", "ask_wall_ratio",
    "bid_qty_added", "bid_qty_removed", "ask_qty_added", "ask_qty_removed",
    "bid_add_count", "bid_remove_count", "ask_add_count", "ask_remove_count",
    "ofi", "mid_price_change", "imbalance_l10_change", "imbalance_l50_change",
]


def _row_to_list(row: dict[str, Any], cols: list[str]) -> list[Any]:
    return [row[c] for c in cols]


def insert_features(client: Any, rows: list[dict[str, Any]], batch_size: int = 50_000) -> int:
    """Insert feature rows, return total rows inserted."""
    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        data = [_row_to_list(r, FEATURE_COLS) for r in batch]
        client.insert(FEATURES_TABLE, data, column_names=FEATURE_COLS)
        total += len(batch)
    return total


def upsert_manifest(client: Any, row: dict[str, Any]) -> None:
    data = [_row_to_list(row, MANIFEST_COLS)]
    client.insert(MANIFEST_TABLE, data, column_names=MANIFEST_COLS)


def optimize_tables(client: Any) -> None:
    """Force merge to deduplicate ReplacingMergeTree rows."""
    client.command(f"OPTIMIZE TABLE {MANIFEST_TABLE} FINAL")
    client.command(f"OPTIMIZE TABLE {FEATURES_TABLE} FINAL")


def count_features(client: Any, symbol: str, source_date: date) -> int:
    dt_start = datetime(source_date.year, source_date.month, source_date.day, tzinfo=timezone.utc)
    dt_end = datetime(source_date.year, source_date.month, source_date.day + 1
                      if source_date.day < 28 else source_date.month + 1, 1, 1,
                      tzinfo=timezone.utc)
    # simpler: just query by date partition
    r = client.query(
        f"SELECT count() FROM {FEATURES_TABLE} FINAL "
        f"WHERE symbol = %(sym)s AND toDate(bucket_start) = %(d)s",
        parameters={"sym": symbol, "d": source_date.isoformat()},
    )
    return r.result_rows[0][0]

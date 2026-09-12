"""Isolated ClickHouse bronze Full-OB pilot (UTC/ns-safe schema v1_2).

v1 / v1_1 tables are intentionally left untouched (known −7200s DateTime64 bug).
"""

from __future__ import annotations

AUDIT_ID = "FULL_OB_CLICKHOUSE_UTC_NS_PREFIX_V1_2"
SCHEMA_VERSION = "raw_full_ob_continuous_pilot_v1_2"
DATABASE = "research_full_ob_continuous_pilot_v1"

# New tables only — do not reuse broken v1 event/ledger tables.
EVENTS_TABLE = "raw_full_ob_events_pilot_v1_2"
LEDGER_TABLE = "raw_full_ob_import_segments_pilot_v1_2"

PILOT_SYMBOL = "BTCUSDT"
# Analysis minute (Silver metrics / parity target).
PILOT_WINDOW_START = "2026-09-06T20:19:00.000000000Z"
PILOT_WINDOW_END = "2026-09-06T20:20:00.000000000Z"
# Bronze prefix including last full checkpoint before analysis start.
PREFIX_WINDOW_START = "2026-09-06T20:14:59.873000000Z"
PREFIX_WINDOW_END = "2026-09-06T20:20:00.000000000Z"
# Canonical 1-based archive ordinal of the full checkpoint at PREFIX_WINDOW_START.
# (Same-ns delta at ordinal 4503 precedes this checkpoint.)
EXPECTED_START_CHECKPOINT_ORDINAL = 4504

PILOT_SEGMENT = (
    "/home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_shadow/"
    "full_ob_v1/BTCUSDT/2026/09/06/"
    "BTCUSDT_20260906T200000Z_eb3cbb7ffd1f_full_ob_continuous_raw_archive_v1.ndjson.zst"
)

MAX_WALL_CLOCK_S = 300
MAX_RSS_BYTES = 2 * 1024 * 1024 * 1024
BATCH_SIZE = 200  # staging native insert; checkpoint payloads stay out of SQL VALUES
EXPECTED_FORMAT_VERSION = "full_ob_continuous_raw_archive_v1"

__all__ = [
    "AUDIT_ID",
    "SCHEMA_VERSION",
    "DATABASE",
    "EVENTS_TABLE",
    "LEDGER_TABLE",
    "PILOT_SYMBOL",
    "PILOT_WINDOW_START",
    "PILOT_WINDOW_END",
    "PREFIX_WINDOW_START",
    "PREFIX_WINDOW_END",
    "EXPECTED_START_CHECKPOINT_ORDINAL",
    "PILOT_SEGMENT",
    "MAX_WALL_CLOCK_S",
    "MAX_RSS_BYTES",
    "BATCH_SIZE",
]

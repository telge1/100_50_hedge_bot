"""Silver pilot constants for Full-OB continuous bronze → checkpoints/LC/metrics (v1_2)."""

from __future__ import annotations

SILVER_SCHEMA_VERSION = "ob_silver_pilot_v1_2"
SILVER_REPLAY_CONTRACT = (
    "FullBookState.apply_snapshot/apply_delta + drilldown epoch-on-checkpoint; "
    "level_changes sparse (analysis window only); "
    "metrics_100ms event_time_strict_lt bucket_end analysis window only; "
    "single-segment source order = record_ordinal; multi-segment source order "
    "= (canonical_segment_chain_index, record_ordinal), never SHA-primary; "
    "DateTime64 via fromUnixTimestamp64Nano(ns,'UTC')"
)
BUCKET_MS = 100
NEAR_BPS = "0_2"

CHECKPOINTS_TABLE = "ob_checkpoints_pilot_v1_2"
LEVEL_CHANGES_TABLE = "ob_level_changes_pilot_v1_2"
METRICS_TABLE = "ob_metrics_100ms_pilot_v1_2"
BUILDS_TABLE = "ob_silver_builds_pilot_v1_2"

# Known Episode-1 corrected SMS1 persist (read-only reference, never rebuilt here).
DEFAULT_REFERENCE_STATES = (
    "/home/telgenbuescher/projects/orderbook_analyse/obfull_research_engine/results/"
    "level_first_episode1_corrected_sms1_persist_v1/BTCUSDT/csp1_13058debfd4bba04/"
    "build_primary/states_100ms.jsonl.zst"
)
DEFAULT_REFERENCE_LEVEL_CHANGES = (
    "/home/telgenbuescher/projects/orderbook_analyse/obfull_research_engine/results/"
    "level_first_episode1_corrected_sms1_persist_v1/BTCUSDT/csp1_13058debfd4bba04/"
    "build_primary/level_changes.jsonl.zst"
)
DEFAULT_REFERENCE_BOOK_RESETS = (
    "/home/telgenbuescher/projects/orderbook_analyse/obfull_research_engine/results/"
    "level_first_episode1_corrected_sms1_persist_v1/BTCUSDT/csp1_13058debfd4bba04/"
    "build_primary/book_resets.jsonl.zst"
)

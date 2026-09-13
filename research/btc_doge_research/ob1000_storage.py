"""Column contract for research_ob1000_snapshots_1s (CH query surface).

Mirrors OB200 1s snapshot layout with genuine_depth up to 1000.
Materialization from FS ``ob1000_v1`` raw archive is a separate loader step.
"""

from __future__ import annotations

OB1000_SNAPSHOT_1S_COLUMNS: tuple[str, ...] = (
    "symbol",
    "snapshot_ts",
    "producer_id",
    "bid_price_ticks",
    "bid_quantities",
    "ask_price_ticks",
    "ask_quantities",
    "best_bid",
    "best_ask",
    "mid",
    "spread",
    "bid_level_count",
    "ask_level_count",
    "genuine_depth",
    "reconstruction_clock",
    "source_event_count",
    "source_event_time",
    "source_update_id",
    "ordering_quality",
    "source_fingerprint",
    "contract_version",
    "source_semantics_version",
    "build_id",
    "coverage_status",
    "computed_at",
)

OB1000_TABLE = "research_ob1000_snapshots_1s"
OB1000_PRODUCER_ID = "OB1000_V1_RAW_SHADOW"
OB1000_MAX_DEPTH = 1000

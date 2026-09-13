"""Frozen Phase-1 contract constants.

This module defines a factual episode dataset. It contains no bias, prediction,
trading, or order-execution semantics.
"""

from __future__ import annotations

from enum import Enum
from typing import Final

CONTRACT_VERSION: Final = "liquidity_destination_episode_contract_v1"
BUILDER_VERSION: Final = "liquidity_destination_episode_builder_v1"
TARGET_SOURCE: Final = "LLD_POOL"
TARGET_CONTRACT_VERSION: Final = "liquidity_pool_signal/canonical_v1"
TARGET_TIMEFRAME: Final = "5m"
PRICE_SOURCE: Final = "btc_doge_research.research_public_trades"
PRICE_BUCKET: Final = "1s"
GRID_SECONDS: Final = 60
WARMUP_SECONDS: Final = 300
MIN_TARGET_PERSISTENCE_SECONDS: Final = 60
AMBIGUITY_MILLISECONDS: Final = 1000
MAX_CANDIDATE_WINDOW_SECONDS: Final = 2 * 60 * 60


class Outcome(str, Enum):
    UPPER_FIRST = "UPPER_FIRST"
    LOWER_FIRST = "LOWER_FIRST"
    NEITHER = "NEITHER_WITHIN_HORIZON"
    AMBIGUOUS = "SIMULTANEOUS_OR_AMBIGUOUS"
    INELIGIBLE = "INELIGIBLE_DATA"


class Eligibility(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"


class ExclusionReason(str, Enum):
    MISSING_UPPER_TARGET = "MISSING_UPPER_TARGET"
    MISSING_LOWER_TARGET = "MISSING_LOWER_TARGET"
    TARGET_ALREADY_TOUCHED_AT_T0 = "TARGET_ALREADY_TOUCHED_AT_T0"
    TARGETS_OVERLAP = "TARGETS_OVERLAP"
    TARGET_ORDER_INVALID = "TARGET_ORDER_INVALID"
    PRICE_MISSING_AT_T0 = "PRICE_MISSING_AT_T0"
    FUTURE_PRICE_PATH_INCOMPLETE = "FUTURE_PRICE_PATH_INCOMPLETE"
    INSUFFICIENT_WARMUP = "INSUFFICIENT_WARMUP"
    SOURCE_COVERAGE_INCOMPLETE = "SOURCE_COVERAGE_INCOMPLETE"
    ORDERBOOK_DEPTH_UNPROVEN = "ORDERBOOK_DEPTH_UNPROVEN"
    SEQUENCE_GAP = "SEQUENCE_GAP"
    PUBLIC_TRADES_MISSING_IF_MANDATORY = "PUBLIC_TRADES_MISSING_IF_MANDATORY"
    HORIZON_NOT_CLOSED = "HORIZON_NOT_CLOSED"
    SIMULTANEOUS_TOUCH = "SIMULTANEOUS_TOUCH"
    CAUSALITY_VIOLATION = "CAUSALITY_VIOLATION"
    DUPLICATE_OR_OVERLAPPING_EPISODE = "DUPLICATE_OR_OVERLAPPING_EPISODE"
    UNSUPPORTED_TARGET_SOURCE = "UNSUPPORTED_TARGET_SOURCE"


EPISODE_COLUMNS: Final = (
    "episode_id",
    "symbol",
    "t0_utc",
    "knowledge_cutoff_utc",
    "horizon_end_utc",
    "price_t0",
    "upper_target_id",
    "upper_target_lower_price",
    "upper_target_upper_price",
    "upper_touch_price",
    "upper_target_available_at",
    "lower_target_id",
    "lower_target_lower_price",
    "lower_target_upper_price",
    "lower_touch_price",
    "lower_target_available_at",
    "distance_upper_bps",
    "distance_lower_bps",
    "target_source",
    "target_contract_version",
    "outcome",
    "first_touch_utc",
    "upper_touch_utc",
    "lower_touch_utc",
    "eligibility",
    "exclusion_reason",
    "source_coverage_start_utc",
    "source_coverage_end_utc",
    "canonical_snapshot_sha256",
    "builder_version",
    "contract_version",
)

EXCLUDED_COLUMNS: Final = (
    "symbol",
    "t0_utc",
    "eligibility",
    "outcome",
    "exclusion_reason",
    "detail",
    "target_source",
    "builder_version",
    "contract_version",
)


def contract_dict() -> dict:
    return {
        "contract_version": CONTRACT_VERSION,
        "builder_version": BUILDER_VERSION,
        "research_only": True,
        "target_source": TARGET_SOURCE,
        "target_contract_version": TARGET_CONTRACT_VERSION,
        "target_timeframe": TARGET_TIMEFRAME,
        "price_source": PRICE_SOURCE,
        "price_bucket": PRICE_BUCKET,
        "grid_seconds": GRID_SECONDS,
        "warmup_seconds": WARMUP_SECONDS,
        "minimum_target_persistence_seconds": MIN_TARGET_PERSISTENCE_SECONDS,
        "ambiguity_milliseconds": AMBIGUITY_MILLISECONDS,
        "touch_semantics": {
            "upper": "bucket_high >= frozen upper block lower edge",
            "lower": "bucket_low <= frozen lower block upper edge",
            "same_or_adjacent_within_ms": "SIMULTANEOUS_OR_AMBIGUOUS",
            "acceptance": "not evaluated",
        },
        "overlap_policy": "one active episode per symbol; next T0 >= prior outcome time or horizon end",
        "allowed_outcomes": [x.value for x in Outcome],
        "allowed_exclusion_reasons": [x.value for x in ExclusionReason],
        "forbidden_capabilities": [
            "bias",
            "probability",
            "prediction",
            "now",
            "trading",
            "orders",
            "dashboard",
            "full_ob_invention",
        ],
    }

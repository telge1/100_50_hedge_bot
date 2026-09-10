"""Versioned wall/defense-chain OutcomeFacts + OutcomeLabel contract.

Verdict candidate: WALL_DEFENSE_OUTCOME_CONTRACT_V1_PROVEN

This package defines WHAT will be measured and HOW it will be named.
Episode 1 is a schema/mechanics test only — no thresholds optimized on
Episode-1 outcomes.

Features and outcomes are strictly separated. Outcome data must never
flow back into touch/detection/relevance/chain/QDH/aggressor/price-response
or signal feature construction.
"""

from __future__ import annotations

import hashlib
import json

AUDIT_ID = "WALL_DEFENSE_OUTCOME_CONTRACT_V1"
CONTRACT_VERSION = "wall_defense_outcome_contract_v1"
SCHEMA_VERSION = "wall_defense_outcome_contract_v1"
RUN_PREFIX = "odc1_"

# Fixed horizons (ms) — changing these requires a new contract version.
HORIZON_MS = (
    1_000,
    3_000,
    5_000,
    10_000,
    15_000,
    30_000,
    60_000,
    120_000,
    300_000,
    900_000,
    1_800_000,
)

ANCHOR_TYPES = (
    "ZONE_FIRST_TOUCH",
    "WALL_FIRST_TOUCH",
    "FIRST_JOINT_BREACH",
    "DETECTION",
    "DECISION_TIME",
)

# Mechanical labels — not trading classes.
LABEL_NO_WALL_TOUCH = "NO_WALL_TOUCH"
LABEL_ATTACK_NO_JOINT = "WALL_ATTACK_NO_JOINT_BREACH"
LABEL_JOINT_ATTACK_AT_H = "JOINT_BREACH_ATTACK_SIDE_AT_HORIZON"
LABEL_JOINT_RECLAIM_AT_H = "JOINT_BREACH_RECLAIM_DEFENDER_SIDE_AT_HORIZON"
LABEL_LAYERED_RECLAIM = "LAYERED_DEFENSE_RECLAIM_AT_HORIZON"
LABEL_MULTI_RECROSS = "MULTIPLE_RECROSS_UNRESOLVED"
LABEL_PULLED = "PULLED_WITHOUT_MATCHING_EXECUTION"
LABEL_NO_RESOLUTION = "NO_RESOLUTION_AT_HORIZON"
LABEL_CENSORED = "CENSORED"
LABEL_INCONCLUSIVE = "INCONCLUSIVE"

MECHANICAL_LABELS = (
    LABEL_NO_WALL_TOUCH,
    LABEL_ATTACK_NO_JOINT,
    LABEL_JOINT_ATTACK_AT_H,
    LABEL_JOINT_RECLAIM_AT_H,
    LABEL_LAYERED_RECLAIM,
    LABEL_MULTI_RECROSS,
    LABEL_PULLED,
    LABEL_NO_RESOLUTION,
    LABEL_CENSORED,
    LABEL_INCONCLUSIVE,
)

# Censor reasons (contract vocabulary).
CENSOR_EPOCH_BOUNDARY = "EPOCH_BOUNDARY"
CENSOR_SEQUENCE_GAP = "SEQUENCE_GAP"
CENSOR_BOOK_COVERAGE_MISSING = "BOOK_COVERAGE_MISSING"
CENSOR_BBO_INVALID = "BBO_INVALID"
CENSOR_RAW_DATA_END = "RAW_DATA_END"
CENSOR_HORIZON_NOT_REACHED = "HORIZON_NOT_REACHED"
CENSOR_DETECTION_NOT_AVAILABLE = "DETECTION_NOT_AVAILABLE"
CENSOR_WALL_GENERATION_INVALID = "WALL_GENERATION_INVALID"
CENSOR_ZONE_NOT_AVAILABLE = "ZONE_NOT_AVAILABLE"
CENSOR_UNKNOWN = "UNKNOWN_COVERAGE_FAILURE"

CENSOR_REASONS = (
    CENSOR_EPOCH_BOUNDARY,
    CENSOR_SEQUENCE_GAP,
    CENSOR_BOOK_COVERAGE_MISSING,
    CENSOR_BBO_INVALID,
    CENSOR_RAW_DATA_END,
    CENSOR_HORIZON_NOT_REACHED,
    CENSOR_DETECTION_NOT_AVAILABLE,
    CENSOR_WALL_GENERATION_INVALID,
    CENSOR_ZONE_NOT_AVAILABLE,
    CENSOR_UNKNOWN,
)

# Research default cluster window — NOT profit-optimized.
ATTACK_CLUSTER_GAP_MS_DEFAULT = 60_000

# Pull share is stored raw; no Episode-1 threshold selection.
PULL_SHARE_RAW_FIELD_ONLY = True

TICK_SIZE = 0.1
TIME_BASIS = "EVENT_TIME_ONLY"
ALLOW_CLICKHOUSE_WRITES = False

VERDICT_OK = "WALL_DEFENSE_OUTCOME_CONTRACT_V1_PROVEN"

FROZEN_DEFENSE_CHAIN_RUN = (
    "results/level_first_episode1_defense_chain_v1/BTCUSDT/dch1_9bb0b8ff5ce8a"
)
FROZEN_PRICE_RESPONSE_RUN = (
    "results/level_first_episode1_price_response_reclaim_v1/BTCUSDT/prr1_5c62bb55455da"
)
FROZEN_HANDOFF_PATH = (
    "results/level_first_episode1_detection_to_wall_flow_integration_v1/"
    "BTCUSDT/d2w1_eee5d0eefaffa/episode1_handoff.json"
)
EPOCH4_COVERAGE_END = "2026-09-06T20:19:59.800Z"
ASK_WALL_BREACH = "2026-09-06T20:19:41.900Z"

# Forbidden feature→outcome leakage substrings for audits.
FORBIDDEN_FEATURE_OUTCOME_COLUMNS = (
    "outcome_label",
    "mfe_ticks",
    "mae_ticks",
    "pnl",
    "trade_result",
    "entry_price",
    "exit_price",
)


def contract_definition() -> dict:
    return {
        "outcome_contract_version": CONTRACT_VERSION,
        "horizons_ms": list(HORIZON_MS),
        "anchor_types": list(ANCHOR_TYPES),
        "mechanical_labels": list(MECHANICAL_LABELS),
        "censor_reasons": list(CENSOR_REASONS),
        "attack_cluster_gap_ms_default": ATTACK_CLUSTER_GAP_MS_DEFAULT,
        "pull_share_raw_only": PULL_SHARE_RAW_FIELD_ONLY,
        "side_semantics": {
            "ask_wall": "rising_price_is_positive_attack",
            "bid_wall": "falling_price_is_positive_attack",
            "fields": [
                "attack_progress_ticks",
                "defender_progress_ticks",
                "attack_progress_bps",
                "defender_progress_bps",
            ],
        },
        "separation": {
            "OutcomeFacts": "raw non-interpreted future measurements",
            "OutcomeLabel": "deterministic names from versioned OutcomeFacts only",
            "feature_leakage": "forbidden",
        },
        "not_trading_classes": [
            "SUSTAINABLE_ABSORPTION",
            "WALL_BREAK_CONTINUATION",
        ],
        "episode1_role": "schema_mechanics_test_only",
        "note": (
            "Changing horizons, labels, side semantics, censoring, or anchors "
            "requires a new contract version and hash."
        ),
    }


def outcome_contract_hash() -> str:
    payload = json.dumps(contract_definition(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


CONTRACT_HASH = outcome_contract_hash()

__all__ = [
    "CONTRACT_VERSION",
    "CONTRACT_HASH",
    "VERDICT_OK",
    "HORIZON_MS",
    "MECHANICAL_LABELS",
    "contract_definition",
    "outcome_contract_hash",
]

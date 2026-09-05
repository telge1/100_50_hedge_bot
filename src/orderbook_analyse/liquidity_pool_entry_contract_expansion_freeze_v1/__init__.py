"""Outcome-blind frozen expansion sample for Liquidity Pool Entry Contract V1."""

SCHEMA_VERSION = "liquidity_pool_entry_contract_expansion_freeze/v1"
RESULTS_DIR_REL = "results/liquidity_pool_entry_contract_expansion_freeze_v1"
SAMPLING_SEED = "LP_ENTRY_CONTRACT_V1_EXPANSION_24"
TARGET_COUNT = 24
CASE_SEQUENCE_FREEZE_SHA256 = (
    "5ec44b95273af34508c327c841d5734e4ff1193caacb332d1f9d1e2cf79140d8"
)
ENTRY_CONTRACT_FREEZE_SHA256 = (
    "76b79cce5ceac816feade974521f0b4f876adb5ab6960e54d6e9498b93e97494"
)
STRATEGY_CONFIG_SHA256 = (
    "905c8f6cd3b642cb356fe80baab64a80be231a905645a16b2e21a7b79a870050"
)
STRATEGY_CONFIG_REL = (
    "strategies/strategy_lab/liquidity_pool_market_response_strategy_v0.yaml"
)

FORBIDDEN_FIELD_SUBSTR = (
    "outcome",
    "verdict",
    "pnl",
    "mfe",
    "mae",
    "return_",
    "_return",
    "reaction_class",
    "evidence_class",
    "trade_no",
    "no_trade",
    "micro_pass",
    "room_pass",
    "contest",
)

OUTCOME_LIKE_SOURCE_COLUMNS = (
    "additional_wall_appeared_after_arrival",
    "arrival_wall_persisted_post_arrival",
    "strictly_post_arrival_wall_count",
)

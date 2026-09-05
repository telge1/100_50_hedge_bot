"""Resumable outcome-blind Entry Contract batch runner for Expansion v3."""

FORMAT_VERSION = "liquidity_pool_entry_contract_batch/v1"
RESULTS_DIR_REL = "results/liquidity_pool_entry_contract_expansion_batch_v1"
V3_FREEZE_REL = "results/liquidity_pool_entry_contract_expansion_freeze_v3"
V3_FREEZE_FILE = "frozen_expansion_cases_v3.json"
EXPECTED_V3_HASH = "48b5a69f54603e2fa55f81e887d6f45b441878c5f3493ab936b5d849e9614cd5"
EXPECTED_ENTRY_CONTRACT_HASH = (
    "76b79cce5ceac816feade974521f0b4f876adb5ab6960e54d6e9498b93e97494"
)
EXPECTED_STRATEGY_CONFIG_HASH = (
    "905c8f6cd3b642cb356fe80baab64a80be231a905645a16b2e21a7b79a870050"
)
EXPECTED_CASE_SEQUENCE_HASH = (
    "5ec44b95273af34508c327c841d5734e4ff1193caacb332d1f9d1e2cf79140d8"
)
STRATEGY_CONFIG_REL = (
    "strategies/strategy_lab/liquidity_pool_market_response_strategy_v0.yaml"
)
ENTRY_CONTRACT_FREEZE_REL = "results/liquidity_pool_entry_contract_freeze_v1"
TARGET_MECHANICAL_COMPLETE = 24
SMOKE_ASK_CASE_ID = "EXP_01"
SMOKE_BID_CASE_ID = "EXP_03"
CONCURRENCY = 1
STALE_RUNNING_S = 3600

STATUS_PENDING = "PENDING"
STATUS_RUNNING = "RUNNING"
STATUS_MECHANICAL_COMPLETE = "MECHANICAL_COMPLETE"
STATUS_FAILED_RETRYABLE = "FAILED_RETRYABLE"
STATUS_FAILED_FINAL = "FAILED_FINAL"
STATUS_UNBLINDED = "UNBLINDED"

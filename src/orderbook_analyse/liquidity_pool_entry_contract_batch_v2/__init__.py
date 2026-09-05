"""Resumable mechanical-only Entry Contract batch runner V2 (Expansion binding v4)."""

FORMAT_VERSION = "liquidity_pool_entry_contract_batch/v2"
RESULTS_DIR_REL = "results/liquidity_pool_entry_contract_expansion_batch_v2"
V4_FREEZE_REL = "results/liquidity_pool_entry_contract_expansion_freeze_v4"
V4_FREEZE_FILE = "frozen_expansion_cases_v4.json"
V3_FREEZE_REL = "results/liquidity_pool_entry_contract_expansion_freeze_v3"
V3_FREEZE_FILE = "frozen_expansion_cases_v3.json"
ENTRY_CONTRACT_V2_FREEZE_REL = "results/liquidity_pool_entry_contract_freeze_v2"
ENTRY_CONTRACT_V2_FILE = "entry_contract_v2.json"
STRATEGY_CONFIG_REL = (
    "strategies/strategy_lab/liquidity_pool_market_response_strategy_v0.yaml"
)
EXPECTED_V2_HASH = "f9d6006eec4761eeda06b72cd0ec3d07eb8a7830597fe13ed1ee926e78f763f5"
EXPECTED_V4_HASH = "f0253eafabcaafc858ffbdcf74443aae23ec57e4b586e7d9cafefc0f57859551"
EXPECTED_V3_HASH = "48b5a69f54603e2fa55f81e887d6f45b441878c5f3493ab936b5d849e9614cd5"
EXPECTED_STRATEGY_CONFIG_HASH = (
    "905c8f6cd3b642cb356fe80baab64a80be231a905645a16b2e21a7b79a870050"
)
EXPECTED_V1_PREDECESSOR = (
    "76b79cce5ceac816feade974521f0b4f876adb5ab6960e54d6e9498b93e97494"
)
DEFAULT_RAW_ROOT_REL = "data/orderbook_raw_shadow/ob200_v3"
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

# Unchanged windows (must match Entry Contract V2 / CASE pipeline)
PRE_S = 30
MAX_POST_S = 30 * 60
ACCEPT_VARIANTS_S = (5, 15, 30, 60)
FLOW_WINDOWS_S = (1, 3, 5, 10, 30)
EDGE_TOL_BPS = 2.0
MAJOR_WALL_RANK = 20
TIMEFRAMES = ("5m", "15m", "30m", "1h")

"""Liquidity Pool Entry Contract V2 — mechanical/unblind separated, ASK/BID symmetric."""

FORMAT_VERSION = "liquidity_pool_entry_contract/v2"
ENTRY_CONTRACT_VERSION = "liquidity_pool_entry_contract/v2"
PREDECESSOR_V1_ENTRY_CONTRACT_SHA256 = (
    "76b79cce5ceac816feade974521f0b4f876adb5ab6960e54d6e9498b93e97494"
)
EXPECTED_STRATEGY_CONFIG_SHA256 = (
    "905c8f6cd3b642cb356fe80baab64a80be231a905645a16b2e21a7b79a870050"
)
STRATEGY_CONFIG_REL = (
    "strategies/strategy_lab/liquidity_pool_market_response_strategy_v0.yaml"
)
RESULTS_FREEZE_REL = "results/liquidity_pool_entry_contract_freeze_v2"
EXPANSION_V3_HASH = "48b5a69f54603e2fa55f81e887d6f45b441878c5f3493ab936b5d849e9614cd5"
EXPANSION_V3_REL = "results/liquidity_pool_entry_contract_expansion_freeze_v3"
EXPANSION_V4_REL = "results/liquidity_pool_entry_contract_expansion_freeze_v4"

# Unchanged windows / acceptance (from V1 CASE pipeline)
PRE_S = 30
MAX_POST_S = 30 * 60
ACCEPT_VARIANTS_S = (5, 15, 30, 60)
FLOW_WINDOWS_S = (1, 3, 5, 10, 30)

VALID_COMBINATIONS = frozenset(
    {
        ("BID", "FROM_ABOVE"),
        ("ASK", "FROM_BELOW"),
    }
)

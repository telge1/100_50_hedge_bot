"""Configurable minimum target-distance room gate for LP market response strategy."""

FORMAT_VERSION = "liquidity_pool_min_target_distance_config/v1"
CANONICAL_STRATEGY_YAML_REL = (
    "strategies/strategy_lab/liquidity_pool_market_response_strategy_v0.yaml"
)
STRATEGY_RESEARCH_DOC_REL = (
    "docs/strategy_research/liquidity_pool_market_response_strategy_v0.md"
)
MAX_MIN_TARGET_DISTANCE_PCT = 10.0

GATE_REASONS = frozenset(
    {
        "TARGET_DISTANCE_SUFFICIENT",
        "TARGET_DISTANCE_BELOW_MINIMUM",
        "ENTRY_INSIDE_OPPOSING_POOL",
        "HTF_OPPOSING_POOL_OVERLAP",
        "TARGET_NOT_OBSERVED",
        "TARGET_NOT_CAUSALLY_AVAILABLE",
        "INVALID_ROOM_GATE_CONFIG",
    }
)

HTF_TIMEFRAMES = frozenset({"15m", "30m", "1h"})

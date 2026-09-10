"""Causal multiscale Market-Profile context — dashboard formulas, no fork."""

from __future__ import annotations

ADAPTER_VERSION = "OBFULL_MARKET_PROFILE_MULTISCALE_CONTEXT_V1"
CONTRACT_VERSION = "1.0.0"
CONTRACT_SCHEMA = "market_profile_multiscale_context_v1"
STATUS = "RESEARCH_CONTEXT_ONLY"

TIMEFRAMES = ("15m", "30m", "1h", "4h")
TIMEFRAME_ROLES = {
    "4h": "macro_location",
    "1h": "structural_location",
    "30m": "decision_edge",
    "15m": "timing_context",
}

# Versioned proximity: one dashboard price-step (bin). Not fitted on outcomes.
PROXIMITY_BINS = 1
CONFLUENCE_BINS = 1
TOUCH_FRAC_OF_STEP = 0.5

USE_FINAL = False  # dashboard default for live profiles
VALUE_AREA_PCT_SOURCE = "market_profile_v1.service.DEFAULT_VALUE_AREA_PCT"
TARGET_BINS_SOURCE = "market_profile_v1.service.DEFAULT_TARGET_BINS"

VERDICT_PASS = "OBFULL_MARKET_PROFILE_MULTISCALE_CONTEXT_V1_PASS"
VERDICT_PASS_LIMITS = "OBFULL_MARKET_PROFILE_MULTISCALE_CONTEXT_V1_PASS_WITH_LIMITS"
VERDICT_FAIL = "OBFULL_MARKET_PROFILE_MULTISCALE_CONTEXT_V1_FAIL"

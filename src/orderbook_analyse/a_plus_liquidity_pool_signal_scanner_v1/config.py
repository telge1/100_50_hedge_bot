"""Research-only A+ pool signal scanner configuration."""

from __future__ import annotations

SCANNER_VERSION = "A_PLUS_LIQUIDITY_POOL_SIGNAL_SCANNER_V2"
SCANNER_ID = "a_plus_liquidity_pool_signal_scanner_v1"

# V2 contract — human-defined, not outcome-tuned
ENTRY_FRACTION_FROM_LOWER = 0.60
NEARBY_POOL_DISTANCE_ATR = 2.0
SMALL_POOL_WIDTH_ATR = 0.25
TERMINAL_LADDER_TIMEFRAMES = ("1h", "15m")

SETUP_TYPES = (
    "A_PLUS_PULLBACK_SHORT",
    "A_PLUS_TERMINAL_POOL_LONG",
    "A_PLUS_PULLBACK_LONG",
    "A_PLUS_TERMINAL_POOL_SHORT",
)

# Causal timeframe roles
TF_MACRO = "1h"
TF_LIQUIDITY = "30m"
TF_ENTRY_POOL = "15m"
TF_STRUCTURE = "5m"
TF_CONFIRM = "1m"

TIMEFRAME_ROLES = {
    TF_MACRO: "macro_pool_structure_terminal",
    TF_LIQUIDITY: "liquidity_asymmetry_targets",
    TF_ENTRY_POOL: "local_entry_pools",
    TF_STRUCTURE: "trend_pullback_structure",
    TF_CONFIRM: "final_1m_confirmation",
}

VERIFIED_TICK_SYMBOLS = frozenset({"BTCUSDT", "DOGEUSDT", "XRPUSDT"})

# Research parameters — not tuned on reference examples
MIN_COMPONENT_COUNT = 3
MIN_POOL_STRENGTH = 0.0
APPROACH_ATR_MULT = 1.5
MIN_TARGET_DISTANCE_ATR = 0.75
MIN_NET_REWARD_DISTANCE_TICKS = 20
MIN_GROSS_RR = 1.2
STOP_ATR_BUFFER = 0.15
TARGET_TICK_BUFFER = 2
CANDIDATE_EXPIRY_MINUTES = 180
SHADOW_OUTCOME_HORIZON_MINUTES = 240

DEFAULT_OUT_DIR = (
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "a_plus_liquidity_pool_signal_scanner_v1"
)

SMOKE_SYMBOLS = ("DOGEUSDT",)

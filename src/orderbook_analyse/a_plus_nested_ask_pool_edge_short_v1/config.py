"""A_PLUS_NESTED_ASK_POOL_EDGE_SHORT_V1 — research-only config.

No DOGE-specific prices or times. Thresholds are declared research defaults,
not tuned on the reference screenshot.
"""

from __future__ import annotations

SETUP_TYPE = "A_PLUS_NESTED_ASK_POOL_EDGE_SHORT_V1"
SETUP_VERSION = "v1"

TF_CHILD = "1m"
TF_PARENT_5M = "5m"
TF_PARENT_15M = "15m"
TIMEFRAMES = (TF_CHILD, TF_PARENT_5M, TF_PARENT_15M)

# Stop buffers — reuse scanner research defaults, not DOGE-tuned
STOP_TICK_BUFFER = 2
STOP_ATR_BUFFER = 0.15
MAX_STOP_DISTANCE_PCT = 1.0  # hard gate: STOP_TOO_WIDE

# Cost baselines (round-trip, percent of notional)
ROUNDTRIP_COST_PCT_BASELINE = 0.15
ROUNDTRIP_COST_PCT_SENSITIVITY = (0.11, 0.15, 0.20)

# Approach: price must be below child lower edge (approaching from below)
# Max distance in ATR to consider "approaching" — descriptive filter, not outcome-tuned
APPROACH_MAX_ATR = 3.0

# Spatial separation for next ask above parent zone
GAP_SEPARATION_TICKS = 2

# Outcome horizon after fill
OUTCOME_HORIZON_MINUTES = 240

# Descriptive gap buckets (ATR)
GAP_ATR_BUCKETS = (
    (0.0, 0.25, "<0.25ATR"),
    (0.25, 0.50, "0.25-0.50ATR"),
    (0.50, 1.00, "0.50-1.00ATR"),
    (1.00, 2.00, "1.00-2.00ATR"),
    (2.00, float("inf"), ">=2.00ATR"),
)

GAP_PCT_BUCKETS = (
    (0.0, 0.25, "<0.25pct"),
    (0.25, 0.50, "0.25-0.50pct"),
    (0.50, 1.00, "0.50-1.00pct"),
    (1.00, float("inf"), ">=1.00pct"),
)

DEFAULT_OUT_DIR = (
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "a_plus_nested_ask_pool_edge_short_v1"
)

# Reference-case window (parity audit only — not hardcoded into detection)
REFERENCE_SYMBOL = "DOGEUSDT"
REFERENCE_WINDOW_START = "2026-08-28T14:00:00"
REFERENCE_WINDOW_END = "2026-08-28T15:30:00"
REFERENCE_ENTRY_APPROX = 0.087918  # parity check only
REFERENCE_SL_APPROX = 0.088619  # parity check only

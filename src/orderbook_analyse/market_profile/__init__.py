"""Volume-based market profile for pool/EMA research (read-only).

Builds volume-at-price profiles over explicitly anchored windows, derives the
classic levels (POC, VAH, VAL, HVN, LVN) and classifies the profile shape as
balance or trend. Rendering produces PNG panels; nothing is written back to
ClickHouse and no execution path is touched.

Volume is taker-side aware: `public_trades_canonical.side` is the aggressor,
so every bin carries a buy/sell split that a TPO profile cannot express.
"""

from __future__ import annotations

FORMAT_VERSION = "market_profile/v1"

# Classic market-profile convention: the value area holds 70% of the volume.
DEFAULT_VALUE_AREA_PCT = 0.70

# Target bin count across a window. The price step is derived from the
# window's own high/low, so the same code works for BTC (~1e5 quote) and
# DOGE (~1e-1 quote) without per-symbol tick tables.
DEFAULT_TARGET_BINS = 160

# HVN: local peak with volume >= factor * mean bin volume.
DEFAULT_HVN_FACTOR = 1.35
# LVN: local trough with volume <= factor * mean bin volume.
DEFAULT_LVN_FACTOR = 0.55
# Minimum bin distance between two reported nodes of the same kind.
DEFAULT_NODE_MIN_SEPARATION_BINS = 3
# Volume-profile proxy for a TPO single print: bin volume <= this share of the
# POC bin volume. This is NOT a true single print (which is a TPO-period
# count); it flags prices the market accelerated through.
DEFAULT_SINGLE_PRINT_FRAC = 0.04

# Fixed-duration profile windows aligned to UTC clock boundaries.
PERIOD_ANCHOR_MODES = ("5m", "15m", "30m", "1h", "4h")
# Classic + period modes. Period modes are one profile per UTC block;
# day/session/composite keep their existing semantics.
ANCHOR_MODES = PERIOD_ANCHOR_MODES + ("day", "session", "composite")

# Crypto runs 24/7, so there is no closing auction to anchor on. These are the
# liquidity regimes that stand in for a cash session. `us` is the NYSE
# cash-session analogue (09:30-16:00 ET) and carries the most quote volume.
# Format: (start_hour, start_minute, end_hour, end_minute); end_hour 24 means
# midnight of the following day.
SESSIONS: dict[str, tuple[int, int, int, int]] = {
    "asia": (0, 0, 8, 0),
    "eu": (8, 0, 13, 30),
    "us": (13, 30, 20, 0),
    "late": (20, 0, 24, 0),
}
DEFAULT_SESSIONS = ("asia", "eu", "us", "late")

SHAPE_KINDS = ("BALANCE", "TREND_UP", "TREND_DOWN", "DOUBLE_DISTRIBUTION", "UNCLEAR")
SHAPE_LETTERS = ("D", "P", "b", "B", "-")

CANDLES_FQN = "signal_generator.candles_1m"
TRADES_FQN = "orderbook_analysis.public_trades_canonical"

__all__ = [
    "FORMAT_VERSION",
    "DEFAULT_VALUE_AREA_PCT",
    "DEFAULT_TARGET_BINS",
    "DEFAULT_HVN_FACTOR",
    "DEFAULT_LVN_FACTOR",
    "DEFAULT_NODE_MIN_SEPARATION_BINS",
    "DEFAULT_SINGLE_PRINT_FRAC",
    "ANCHOR_MODES",
    "PERIOD_ANCHOR_MODES",
    "SESSIONS",
    "DEFAULT_SESSIONS",
    "SHAPE_KINDS",
    "SHAPE_LETTERS",
    "CANDLES_FQN",
    "TRADES_FQN",
]

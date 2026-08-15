"""Pull vs consumption deep dive for historical structure-break events."""

from __future__ import annotations

# Transparent matching / classification constants (not outcome-optimized).
ZONE_BPS = 8.0  # structure-level proximity for break-side liquidity
WALL_SELECT_BPS = 15.0  # dominant wall search around level
MATCH_TIME_MS = 750  # OB/trade feed alignment tolerance (documented; not µs-causal)
MATCH_PRICE_BPS = 3.0  # trade price vs wall/level proximity
PULL_RATIO_MAX = 0.30  # matched/removed below → PULL_DOMINANT
CONSUMPTION_RATIO_MIN = 0.70  # matched/removed above → CONSUMPTION_DOMINANT
REFILL_RATIO_MIN = 0.40  # refill vs gross removal for absorption candidate
AGGRESSIVE_FLOW_MIN_FRAC = 0.25  # of peak wall qty as "significant" aggressive flow

MARKER_OFFSETS_S = (
    -60,
    -30,
    -20,
    -10,
    -5,
    -2,
    -1,
    0,
    1,
    2,
    5,
    10,
    20,
    30,
    60,
    120,
)

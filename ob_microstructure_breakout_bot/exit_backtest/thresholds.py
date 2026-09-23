"""Starting thresholds for Phase-1 long exit backtest."""

from __future__ import annotations

# Round-trip fee assumption used for entry skip (gross PnL is still raw).
ROUND_TRIP_FEE_PCT = 0.0011  # 0.11%
# First pool that cannot clear fees + a tiny dust buffer (ignore unless stretch/mass).
FIRST_POOL_FEE_DEAD_PCT = ROUND_TRIP_FEE_PCT + 0.0005  # 0.16%
# When TP is locked to the first pool (void / no second), require meaningful room.
FIRST_POOL_MIN_DIST_PCT = 0.004  # 0.4%

# Stop buffer below last structural low
SL_BUFFER = 0.0015  # 0.15%

# Public-trade delta (notional) — loosened after phase1
DELTA_OK = 100_000.0
DELTA_STRONG = 200_000.0

# Order-book bid/ask ratio in 5bps band
OB_BID_OK = 1.05
OB_BID_STRONG = 1.20

# Weak-delta TP buffer under pool edge
WEAK_DELTA_BUFFER = 0.001  # 0.1%

# Second-pool separation:
# - FAR: still allow continuation if flow is strong
# - VOID: only then force first-pool full-exit bias when flow is also weak
SECOND_POOL_FAR_PCT = 0.010  # 1.0%
SECOND_POOL_VOID_PCT = 0.020  # 2.0%

# Min distance between "first" and "second" meaningful overhead pools.
# On 5m, many micro-stacks sit 0.05–0.1% apart; without this, strong-delta
# continuation only nudges TP by a few bps instead of into the next real cluster.
MIN_MEANINGFUL_POOL_SEP_PCT = 0.008  # 0.8%

# Live cancel window: decide TP shift when price enters this zone *below*
# the first-pool TP, not at the touch itself.
TP_APPROACH_PCT = 0.002  # 0.20% under first.pool bottom

# Phase-C calibrated long TP (EMA59-touch cluster mass study).
# Project TP to heaviest 1m cluster in band when entry flow supports it.
TP_NEAR_NOISE_MAX_DIST_PCT = 1.0
TP_MIN_TARGET_DIST_PCT = 0.8
TP_MAX_TARGET_DIST_PCT = 2.15
TP_MIN_CLUSTER_STRENGTH_SUM = 5.51
TP_MIN_ABS_CONFIRM_DELTA = 71_000.0
TP_MIN_OB_RATIO_LONG = 1.0
TP_REQUIRE_OB = True

# Max hold after entry
MAX_HOLD_HOURS = 48

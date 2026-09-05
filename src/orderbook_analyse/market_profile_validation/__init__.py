"""Does the profile classification predict anything? (research-only, read-only)

Tests three claims that follow from the balance/trend distinction. Each is
stated so that a wrong answer is possible — the point is to find out, not to
confirm.

H1 — value-area edges in balance
    Price reaches the previous window's VAH (from below) or VAL (from above).
    Race: does it revert to that window's POC before breaking the edge by a
    margin? Claim: the rejection rate is higher when the reference window was
    BALANCE than when it was TREND.

H2 — POC as a way station in trend
    Price reaches the previous window's POC. Race: does it then continue in
    the reference window's direction or move against it? Claim: continuation
    beats 50% for TREND windows, and sits near 50% for BALANCE windows, which
    carry no directional information at their equilibrium.

H3 — POC as a magnet in balance
    Simply: is the previous window's POC touched at all during the next
    window? Claim: revisit rate is higher for BALANCE than for TREND, because
    a trending market has left the area.

Causality
    A reference window's profile and class come only from trades inside that
    window. Every outcome is measured strictly after the window closes, on 1m
    candles, and the normaliser is the reference window's own range — known at
    its close. `MarketProfile.naked_poc` is forward-looking by construction
    and is therefore never read as a feature here.
"""

from __future__ import annotations

FORMAT_VERSION = "market_profile_validation/v1"

# Barrier distances as a fraction of the reference window's own range. The
# reference range is causal (known at window close) and scale-free, which
# lets a $80k coin and a $0.2 coin enter the same pool.
DEFAULT_EDGE_MARGIN_FRAC = 0.10
DEFAULT_POC_UNIT_FRAC = 0.15

# Cap on the barrier walk. Default 0 means "until the test window ends".
DEFAULT_MAX_HORIZON_MIN = 0

# Windows whose profile carries too little structure to be worth testing.
MIN_TRADES_PER_WINDOW = 500

OUTCOME_TIMEOUT = "TIMEOUT"
# A single 1m bar that touches both barriers cannot be ordered from OHLC.
# These are counted, reported and excluded from the headline rate; the report
# also shows the worst case where every one of them resolves adversely.
OUTCOME_AMBIGUOUS = "AMBIGUOUS"

H1_REJECTED = "REJECTED"
H1_BROKE = "BROKE"
H2_CONTINUED = "CONTINUED"
H2_REVERSED = "REVERSED"

LEVEL_KINDS = ("VAH", "VAL", "POC")
APPROACH_BELOW = "FROM_BELOW"
APPROACH_ABOVE = "FROM_ABOVE"

DEFAULT_BOOTSTRAP_ITERS = 2000
DEFAULT_SEED = 20260831

__all__ = [
    "FORMAT_VERSION",
    "DEFAULT_EDGE_MARGIN_FRAC",
    "DEFAULT_POC_UNIT_FRAC",
    "DEFAULT_MAX_HORIZON_MIN",
    "MIN_TRADES_PER_WINDOW",
    "OUTCOME_TIMEOUT",
    "OUTCOME_AMBIGUOUS",
    "H1_REJECTED",
    "H1_BROKE",
    "H2_CONTINUED",
    "H2_REVERSED",
    "LEVEL_KINDS",
    "APPROACH_BELOW",
    "APPROACH_ABOVE",
    "DEFAULT_BOOTSTRAP_ITERS",
    "DEFAULT_SEED",
]

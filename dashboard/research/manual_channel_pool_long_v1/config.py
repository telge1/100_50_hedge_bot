"""L1 research pilot parameters (isolated; not production)."""

from __future__ import annotations

from dataclasses import dataclass

FIXED_SL_PCT = 1.20
MIN_POOL_DIST_PCT = 0.50
CHANNEL_NEAR_PCT = 2.0
MAX_CHANNEL_UPPER_SPAN_PCT = 12.0
TOUCH_ZONE_MAX_DIST_PCT = 0.20
TOUCH_RESET_ABOVE_LINE_PCT = 0.30
APPROACH_EPSILON_PCT = 0.05

# L1 V2 entry filters (research; not proven optima)
MIN_CHANNEL_ROOM_PCT = 1.50
MIN_TRADE_PROFIT_PCT = 0.50

# L1 V3 — upper pool landscape (research; provisional thresholds)
ZONE_MERGE_GAP_PCT = 0.15
MIN_UPPER_BAND_WIDTH_PCT = 0.12
MIN_BANDS_PER_SUBSTANTIAL_ZONE = 3
MAX_DISTANCE_TO_FIRST_SUBSTANTIAL_UPPER_PCT = 2.20
MIN_SUBSTANTIAL_UPPER_ZONES = 3

UPPER_MAIN_CHANNEL_LINE_ID = "d-22f8d9f720ce"

PILOT_ALLOWED_LOWER_TREND_IDS: frozenset[str] = frozenset(
    {
        "d-03350098db64",
        "d-0e5e7004dccd",
        "d-1ff495203465",
        "d-248d74050cc8",
        "d-a6d9a05da2f1",
    }
)


@dataclass(frozen=True)
class PilotVariant:
    code: str
    label: str
    channel_room: bool = False
    min_profit: bool = False
    target_reached: bool = False
    pool_landscape: bool = False
    d2_canonical: bool = False
    d2_research_filter: bool = False
    d2_nearness_filter: bool = False
    d2_early_staggering: bool = False


VARIANT_A = PilotVariant("A", "Baseline")
VARIANT_B = PilotVariant("B", "Channel room >= 1.50%", channel_room=True)
VARIANT_C = PilotVariant(
    "C", "Channel room + min effective TP >= 0.50%", channel_room=True, min_profit=True
)
VARIANT_D = PilotVariant(
    "D",
    "All filters incl. channel cycle",
    channel_room=True,
    min_profit=True,
    target_reached=True,
)

ALL_VARIANTS = (VARIANT_A, VARIANT_B, VARIANT_C, VARIANT_D)

# L1 V3 comparison labels (A = prior variant B baseline)
L1_V3_VARIANT_A = VARIANT_B
L1_V3_VARIANT_B = PilotVariant(
    "B+POOL",
    "Channel room 1.50% + upper pool landscape filter (micro proxy)",
    channel_room=True,
    pool_landscape=True,
)

# L1 V3B — canonical D2 gate (Exact Unique-Origin depth >= 2)
D2_MIN_DEPTH = 2
L1_V3B_MICRO = L1_V3_VARIANT_A
L1_V3B_D2_GATE = PilotVariant(
    "D2",
    "Channel room 1.50% + canonical upper D2 gate",
    channel_room=True,
    d2_canonical=True,
)
L1_V3B_D2_RESEARCH = PilotVariant(
    "D2+R",
    "D2 gate + provisional research filter (not micro-calibrated)",
    channel_room=True,
    d2_canonical=True,
    d2_research_filter=True,
)

# L1 V3E — D2 proximity + early channel staggering (research thresholds)
MAX_DISTANCE_TO_FIRST_D2_UPPER_PCT = 2.50
MIN_D2_BANDS_IN_FIRST_CHANNEL_QUARTER = 1

L1_V3E_VARIANT_A = L1_V3B_D2_GATE
L1_V3E_VARIANT_B = PilotVariant(
    "V3E-B",
    "D2 gate + max distance to first upper D2 in channel",
    channel_room=True,
    d2_canonical=True,
    d2_nearness_filter=True,
)
L1_V3E_VARIANT_C = PilotVariant(
    "V3E-C",
    "V3E-B + min D2 bands in first channel quarter",
    channel_room=True,
    d2_canonical=True,
    d2_nearness_filter=True,
    d2_early_staggering=True,
)

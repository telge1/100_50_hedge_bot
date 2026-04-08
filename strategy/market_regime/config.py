from __future__ import annotations

MIN_STD = 1e-6
EPSILON = 1e-9

FAST_LAYER_INTERVAL_SEC = 15
SLOW_LAYER_INTERVAL_SEC = 300
PROFILE_ROLLING_DAYS = 14
DEFAULT_COOLDOWN_FAST_UPDATES = 2
DEFAULT_ROUTED_COOLDOWN_FAST_UPDATES = 2

PRICE_IMPULSE_Z = 1.5
PRICE_FLIP_CONFIRM_Z = 0.5
PRICE_PREV_IMPULSE_Z = 1.0
OI_BUILD_Z = 1.5
OI_FLUSH_Z = -1.5
VOLUME_HIGH_Z = 1.5
VOLUME_EXTREME_Z = 2.0
TRADE_SURGE_Z = 2.0
LARGE_TRADE_Z = 1.5
ORDERFLOW_PUSH_Z = 1.5
MICROBURST_RISK_Z = 1.5
SPREAD_EXPANSION_Z = 1.5
VOLATILITY_EXPANSION_THRESHOLD = 1.25
VOLATILITY_EXPANSION_Z = 1.5
SPREAD_STRESS_THRESHOLD = 1.5
PANIC_LIQ_THRESHOLD = 1.5
PARTICIPATION_WEAK_THRESHOLD = 1.0
DIRTY_BREAKOUT_SPREAD_THRESHOLD = 1.0
LIQ_CLUSTER_Z = 1.5

TREND_PRESSURE_MIN = 35.0
TREND_PRESSURE_MAX = -35.0
TREND_PARTICIPATION_MIN = 35.0
TREND_PARTICIPATION_STRONG_MIN = 20.0
TREND_INSTABILITY_MAX = 70.0
TREND_EXHAUSTION_MAX = 50.0
TREND_EXHAUSTION_STRONG_MAX = 80.0
TREND_STRONG_PRESSURE_MIN = 60.0

REBOUND_PRESSURE_CONFIRM = 20.0
REBOUND_PARTICIPATION_CONFIRM = 40.0
EMERGENCY_INSTABILITY_MIN = 75.0

SLOW_TREND_PRESSURE_MIN = 25.0
SLOW_TREND_PARTICIPATION_MIN = 25.0
SLOW_TREND_EXHAUSTION_MAX = 65.0
SLOW_TRANSITION_EXHAUSTION_MIN = 55.0
SLOW_TRANSITION_PRESSURE_FADE = 10.0

FAST_IMPULSE_PRESSURE_MIN = 35.0
FAST_PULLBACK_PRESSURE_MIN = 20.0
FAST_REVERSAL_PRESSURE_MIN = 30.0
FAST_REVERSAL_PARTICIPATION_MIN = 35.0
FAST_EXHAUSTION_MIN = 50.0
FAST_EMERGENCY_INSTABILITY_MIN = 75.0

SLOW_BIAS_THRESHOLD = 5.0
SLOW_BIAS_CONFIRMATIONS = 2

MID_PULLBACK_PRESSURE_MIN = 3.0
MID_EXHAUSTION_PRESSURE_MIN = 6.0
MID_REVERSAL_PRESSURE_MIN = 10.0
MID_PARTICIPATION_MIN = 8.0
MID_REVERSAL_EXHAUSTION_MIN = 30.0
MID_PULLBACK_MAX_EXHAUSTION = 20.0
MID_EXHAUSTION_MIN_EXHAUSTION = 10.0
MID_REVERSAL_PARTICIPATION_MIN = 15.0
MID_REVERSAL_SLOW_EXHAUSTION_MIN = 20.0

SLOW_STATES = {
    "slow_trend_long",
    "slow_trend_short",
    "slow_range_neutral",
    "slow_transition_long_to_neutral",
    "slow_transition_short_to_neutral",
}

FAST_STATES = {
    "fast_neutral",
    "fast_impulse_long",
    "fast_impulse_short",
    "fast_pullback_short_in_long",
    "fast_pullback_long_in_short",
    "fast_exhaustion_long",
    "fast_exhaustion_short",
    "fast_reversal_attempt_long",
    "fast_reversal_attempt_short",
    "fast_emergency_instability",
}

MID_STATES = {
    "mid_pullback_in_long",
    "mid_pullback_in_short",
    "mid_exhaustion_long",
    "mid_exhaustion_short",
    "mid_reversal_setup_long",
    "mid_reversal_setup_short",
}

ROUTED_STATES = {
    "trend_continuation_long",
    "trend_continuation_short",
    "pullback_in_long_context",
    "pullback_in_short_context",
    "mid_pullback_in_long",
    "mid_pullback_in_short",
    "mid_exhaustion_long",
    "mid_exhaustion_short",
    "mid_reversal_setup_long",
    "mid_reversal_setup_short",
    "range_unclear",
    "reversal_building_long",
    "reversal_building_short",
    "reversal_confirmed_long",
    "reversal_confirmed_short",
    "emergency",
}

ROUTED_REQUIRED_CONFIRMATIONS: dict[str, int] = {
    "trend_continuation_long": 1,
    "trend_continuation_short": 1,
    "pullback_in_long_context": 1,
    "pullback_in_short_context": 1,
    "mid_pullback_in_long": 1,
    "mid_pullback_in_short": 1,
    "mid_exhaustion_long": 1,
    "mid_exhaustion_short": 1,
    "mid_reversal_setup_long": 2,
    "mid_reversal_setup_short": 2,
    "reversal_building_long": 2,
    "reversal_building_short": 2,
    "reversal_confirmed_long": 3,
    "reversal_confirmed_short": 3,
    "emergency": 1,
    "range_unclear": 1,
}

ALLOWED_ROUTED_TRANSITIONS: dict[str, set[str]] = {
    "range_unclear": {
        "trend_continuation_long",
        "trend_continuation_short",
        "pullback_in_long_context",
        "pullback_in_short_context",
        "mid_pullback_in_long",
        "mid_pullback_in_short",
        "mid_exhaustion_long",
        "mid_exhaustion_short",
        "mid_reversal_setup_long",
        "mid_reversal_setup_short",
        "reversal_building_long",
        "reversal_building_short",
        "emergency",
    },
    "trend_continuation_long": {
        "pullback_in_long_context",
        "mid_pullback_in_long",
        "mid_exhaustion_long",
        "mid_reversal_setup_short",
        "range_unclear",
        "emergency",
    },
    "pullback_in_long_context": {
        "trend_continuation_long",
        "mid_pullback_in_long",
        "mid_exhaustion_long",
        "mid_reversal_setup_short",
        "range_unclear",
        "emergency",
    },
    "mid_pullback_in_long": {
        "trend_continuation_long",
        "mid_exhaustion_long",
        "mid_reversal_setup_short",
        "range_unclear",
        "emergency",
    },
    "mid_exhaustion_long": {
        "trend_continuation_long",
        "pullback_in_long_context",
        "mid_reversal_setup_short",
        "reversal_building_short",
        "range_unclear",
        "emergency",
    },
    "mid_reversal_setup_short": {
        "mid_exhaustion_long",
        "reversal_building_short",
        "range_unclear",
        "emergency",
    },
    "trend_continuation_short": {
        "pullback_in_short_context",
        "mid_pullback_in_short",
        "mid_exhaustion_short",
        "mid_reversal_setup_long",
        "range_unclear",
        "emergency",
    },
    "pullback_in_short_context": {
        "trend_continuation_short",
        "mid_pullback_in_short",
        "mid_exhaustion_short",
        "mid_reversal_setup_long",
        "range_unclear",
        "emergency",
    },
    "mid_pullback_in_short": {
        "trend_continuation_short",
        "mid_exhaustion_short",
        "mid_reversal_setup_long",
        "range_unclear",
        "emergency",
    },
    "mid_exhaustion_short": {
        "trend_continuation_short",
        "pullback_in_short_context",
        "mid_reversal_setup_long",
        "reversal_building_long",
        "range_unclear",
        "emergency",
    },
    "mid_reversal_setup_long": {
        "mid_exhaustion_short",
        "reversal_building_long",
        "range_unclear",
        "emergency",
    },
    "reversal_building_long": {
        "reversal_confirmed_long",
        "range_unclear",
        "trend_continuation_long",
        "emergency",
    },
    "reversal_building_short": {
        "reversal_confirmed_short",
        "range_unclear",
        "trend_continuation_short",
        "emergency",
    },
    "reversal_confirmed_long": {
        "trend_continuation_long",
        "pullback_in_long_context",
        "mid_exhaustion_long",
        "emergency",
    },
    "reversal_confirmed_short": {
        "trend_continuation_short",
        "pullback_in_short_context",
        "mid_exhaustion_short",
        "emergency",
    },
    "emergency": {
        "range_unclear",
        "mid_exhaustion_long",
        "mid_exhaustion_short",
        "reversal_building_long",
        "reversal_building_short",
    },
}

REQUIRED_CONFIRMATIONS: dict[str, int] = {
    "trend_long": 2,
    "trend_short": 2,
    "trend_exhaustion_long": 2,
    "trend_exhaustion_short": 2,
    "rebound_start_long": 2,
    "rebound_start_short": 2,
    "rebound_confirmed_long": 3,
    "rebound_confirmed_short": 3,
    "emergency": 1,
}

VALID_STATES = {
    "neutral",
    "trend_long",
    "trend_short",
    "trend_exhaustion_long",
    "trend_exhaustion_short",
    "rebound_start_long",
    "rebound_start_short",
    "rebound_confirmed_long",
    "rebound_confirmed_short",
    "emergency",
}

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "neutral": {"trend_long", "trend_short", "emergency"},
    "trend_long": {"trend_exhaustion_long", "emergency"},
    "trend_short": {"trend_exhaustion_short", "emergency"},
    "trend_exhaustion_long": {"trend_long", "rebound_start_short", "emergency"},
    "trend_exhaustion_short": {"trend_short", "rebound_start_long", "emergency"},
    "rebound_start_long": {"rebound_confirmed_long", "trend_exhaustion_short", "emergency"},
    "rebound_start_short": {"rebound_confirmed_short", "trend_exhaustion_long", "emergency"},
    "rebound_confirmed_long": {"trend_long", "trend_exhaustion_long", "emergency"},
    "rebound_confirmed_short": {"trend_short", "trend_exhaustion_short", "emergency"},
    "emergency": {
        "neutral",
        "trend_exhaustion_long",
        "trend_exhaustion_short",
        "rebound_start_long",
        "rebound_start_short",
    },
}

REGIME_PRIORITY = [
    "emergency",
    "rebound_confirmed_long",
    "rebound_confirmed_short",
    "rebound_start_long",
    "rebound_start_short",
    "trend_exhaustion_long",
    "trend_exhaustion_short",
    "trend_long",
    "trend_short",
]

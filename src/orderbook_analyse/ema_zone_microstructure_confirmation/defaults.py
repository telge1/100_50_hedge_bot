"""Research defaults for EZM — declared / logged, not optimized on BTC windows."""

from __future__ import annotations

# Reuse manual_ema_wall_windows zone geometry (StrategySpec has no atr_fraction binding).
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows import (
    ATR_PERIOD,
    BREAKOUT_HOLD_S,
    PERCENTILE_MIN,
    PERSIST_MIN,
    RECLAIM_HOLD_S,
    REL_SIZE_MIN,
    TICK,
    ZONE_ATR_FRAC,
    ZONE_MIN_TICKS,
)

# YAML plugin config (ema_zone_microstructure_confirmation_v1)
REGIME_SLOPE_LOOKBACK_SHORT = 3
REGIME_SLOPE_LOOKBACK_LONG = 6

# EMA200 is structural only — never equal-weight in short-term regime score
EMA_STRUCTURE_PERIOD = 200
EMA_FAST = 9
EMA_MEDIUM = 20
EMA_SLOW = 59

# Next-zone clearance (research defaults; not event-tuned)
NEXT_ZONE_CLEARANCE_PCT_LO = 0.2
NEXT_ZONE_CLEARANCE_PCT_HI = 0.5
NEXT_ZONE_CLEARANCE_ATR_MULT = 0.5

# Flat-compression block thresholds (same spirit as classify_trend RANGE)
FLAT_SLOPE_ATR_FRAC_EMA20 = 0.02
FLAT_SLOPE_ATR_FRAC_EMA59 = 0.01
NEAR_EMA20_ATR_FRAC = 0.35
# Causal lookback (closed 5m bars) for cross / reorder diagnostics at flat gates
FLAT_LOOKBACK_BARS = 12

# Transition release thresholds (research defaults; not event-tuned).
# transition may proceed only when slope, EMA separation, clear zone, touch,
# and next-zone clearance are all sufficient.
TRANSITION_MIN_ABS_SLOPE_ATR = 0.02  # |ema20_slope_3|/ATR
TRANSITION_MIN_SPREAD_9_59_ATR = 0.15  # |EMA9-EMA59|/ATR
TRANSITION_REQUIRE_STRUCTURE = False  # HH_HL / LH_LL helpful but not mandatory

SHORT_TERM_REGIMES: tuple[str, ...] = (
    "bullish",
    "bearish",
    "transition",
    "range_compression",
    "undetermined",
)

REGISTERED_CANDIDATE_STATES: tuple[str, ...] = (
    "watch_zone",
    "block_flat_compression",
    "wait_microstructure_confirmation",
    "defense_rejection_confirmed",
    "breakout_confirmed",
    "false_breakout_confirmed",
    "wait_next_zone_confirmation",
    "possible_regime_flip",
    "full_regime_flip_confirmed",
    "no_trade",
    "data_incomplete",
)

# Manual comparison hypotheses only — never hardcoded into detector gates
MANUAL_HYPOTHESES: dict[str, str] = {
    "circle_1": "bearish_ema20_pullback_ask_defense",
    "circle_2": "bearish_ema20_pullback_ask_defense",
    "circle_3": "bearish_ema20_pullback_ask_defense",
    "circle_4": "break_attempt_with_reclaim",
    "circle_5": "bearish_ema20_pullback_ask_defense",
    "rectangle": "ask_wall_consumed_breakout_failed_then_ema59",
    "final_circle": "possible_ema59_false_breakout_l2_maybe_incomplete",
}


def methodology_defaults() -> dict:
    return {
        "zone_half_width": f"max({ZONE_ATR_FRAC}*ATR, {ZONE_MIN_TICKS}*tick)",
        "zone_atr_frac": ZONE_ATR_FRAC,
        "zone_min_ticks": ZONE_MIN_TICKS,
        "tick": TICK,
        "atr_period": ATR_PERIOD,
        "regime_slope_lookback_short": REGIME_SLOPE_LOOKBACK_SHORT,
        "regime_slope_lookback_long": REGIME_SLOPE_LOOKBACK_LONG,
        "ema_periods": {
            "fast": EMA_FAST,
            "medium": EMA_MEDIUM,
            "slow": EMA_SLOW,
            "structure": EMA_STRUCTURE_PERIOD,
            "structure_in_short_term_regime_score": False,
        },
        "short_term_regimes": list(SHORT_TERM_REGIMES),
        "regime_gate": {
            "bullish_bearish": "allow_further_checks",
            "range_compression": "hard_block",
            "undetermined": "hard_block_directed_candidates",
            "transition": "release_only_if_slope_separation_clear_zone_touch_clearance",
            "transition_min_abs_slope_atr": TRANSITION_MIN_ABS_SLOPE_ATR,
            "transition_min_spread_9_59_atr": TRANSITION_MIN_SPREAD_9_59_ATR,
            "ema200_role": "sr_clearance_flip_context_not_short_term_score",
        },
        "next_zone_clearance_pct": [NEXT_ZONE_CLEARANCE_PCT_LO, NEXT_ZONE_CLEARANCE_PCT_HI],
        "next_zone_clearance_atr_mult": NEXT_ZONE_CLEARANCE_ATR_MULT,
        "breakout_hold_s": BREAKOUT_HOLD_S,
        "reclaim_hold_s": RECLAIM_HOLD_S,
        "wall_rel_size_min": REL_SIZE_MIN,
        "wall_percentile_min": PERCENTILE_MIN,
        "wall_persist_min": PERSIST_MIN,
        "parameters_not_tuned_on_btc_windows": True,
        "oi_liq_as_hard_gates": False,
        "oi_liq_role": "classifying_features_only",
        "flat_gate_checkpoints": ["watch_at", "touch_at", "decision_at"],
        "flat_lookback_bars": FLAT_LOOKBACK_BARS,
    }

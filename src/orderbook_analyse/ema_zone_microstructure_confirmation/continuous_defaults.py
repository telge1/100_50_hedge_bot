"""Research defaults for continuous discovery — not tuned on manual BTC windows.

Declared here because StrategySpec plugin config currently only binds
regime_slope_lookback_{short,long}. Values are logged in methodology/manifest.
"""

from __future__ import annotations

from orderbook_analyse.ema_zone_microstructure_confirmation.defaults import (
    NEXT_ZONE_CLEARANCE_ATR_MULT,
    NEXT_ZONE_CLEARANCE_PCT_HI,
    NEXT_ZONE_CLEARANCE_PCT_LO,
    ZONE_ATR_FRAC,
    ZONE_MIN_TICKS,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows import (
    BREAKOUT_HOLD_S,
    RECLAIM_HOLD_S,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.proximity import (
    PROXIMITY_WATCH_MAX_PCT,
)

# Approach / watch (causal maxima — not event-centered ±30m)
# V2: proximity watch is percent-of-price (Paket 2C), not 3× half-width.
ZONE_WATCH_DISTANCE_HALFWIDTH_MULT = 3.0  # legacy reference only; V2 uses PROXIMITY_WATCH_MAX_PCT
MAX_WATCH_DURATION_S = 1_800  # drop watch if no touch within 30m
MAX_CONFIRMATION_DURATION_S = 300  # classify using evidence ≤5m after touch
TIMEOUT_S = 600  # no confirmation → timeout / no_trade
COOLDOWN_S = 900  # same zone identity cannot re-fire
REARM_LEAVE_HALFWIDTH_MULT = 1.0  # must leave band by ≥1 half-width before rearm

# Trade impact windows (seconds)
TRADE_WINDOWS_S: tuple[int, ...] = (5, 10, 30, 60, 120, 300)

# Outcome labels (research only; not entries)
OUTCOME_HORIZONS_S: tuple[int, ...] = (60, 180, 300, 600, 900, 1_800, 3_600, 14_400)
GROSS_MFE_THRESHOLD_PCT = 0.15

# Streaming / resources
CHUNK_HOURS = 1
MAX_WORKERS = 1
SAMPLE_MS = 250

SYMBOLS_DEFAULT: tuple[str, ...] = ("BTCUSDT", "DOGEUSDT")
OUT_SUBDIR = "continuous_discovery_v2"
FORMAT_VERSION = "ema_zone_microstructure_confirmation/continuous_discovery/v2"
# Stage A never emits LONG/SHORT; direction only after Stage B microstructure.
STAGE_SEPARATION = "A_ema_setup__B_microstructure_reaction"
RUN_INTENT = "candidate_discovery"


def continuous_research_defaults() -> dict:
    return {
        "source": "research_defaults_not_in_yaml_plugin_config",
        "format_version": FORMAT_VERSION,
        "out_subdir": OUT_SUBDIR,
        "run_intent": RUN_INTENT,
        "stage_separation": STAGE_SEPARATION,
        "yaml_bound_keys": [
            "regime_slope_lookback_short",
            "regime_slope_lookback_long",
        ],
        "zone_atr_frac": ZONE_ATR_FRAC,
        "zone_min_ticks": ZONE_MIN_TICKS,
        "proximity_watch_max_pct": PROXIMITY_WATCH_MAX_PCT,
        "zone_watch_distance_halfwidth_mult_legacy": ZONE_WATCH_DISTANCE_HALFWIDTH_MULT,
        "exact_touch_definition": "mid_inside_zone_band",
        "proximity_never_starts_stage_b": True,
        "proximity_never_emits_directional_marker": True,
        "max_watch_duration_s": MAX_WATCH_DURATION_S,
        "max_confirmation_duration_s": MAX_CONFIRMATION_DURATION_S,
        "timeout_s": TIMEOUT_S,
        "cooldown_s": COOLDOWN_S,
        "rearm_leave_halfwidth_mult": REARM_LEAVE_HALFWIDTH_MULT,
        "breakout_hold_s": BREAKOUT_HOLD_S,
        "reclaim_hold_s": RECLAIM_HOLD_S,
        "next_zone_clearance_pct": [NEXT_ZONE_CLEARANCE_PCT_LO, NEXT_ZONE_CLEARANCE_PCT_HI],
        "next_zone_clearance_atr_mult": NEXT_ZONE_CLEARANCE_ATR_MULT,
        "trade_windows_s": list(TRADE_WINDOWS_S),
        "outcome_horizons_s": list(OUTCOME_HORIZONS_S),
        "gross_mfe_threshold_pct": GROSS_MFE_THRESHOLD_PCT,
        "chunk_hours": CHUNK_HOURS,
        "max_workers": MAX_WORKERS,
        "parameters_not_tuned_on_manual_windows": True,
        "manual_windows_not_used_as_centers": True,
        "approach_role_map": {
            "from_below": "resistance",
            "from_above": "support",
            "inside": "ambiguous",
        },
        "stage_a_never_emits_direction": True,
        "confirmation_modes": {
            "ema_only": "ema_setup_layer_no_trade_direction",
            "ema_plus_microstructure": "microstructure_confirmation_layer",
        },
        "research_chart_layers": {
            "ema_setup": "regime_zones_approach_touch_flat_clearance",
            "microstructure_confirmation": "ob_trades_oi_liq_reaction",
        },
    }

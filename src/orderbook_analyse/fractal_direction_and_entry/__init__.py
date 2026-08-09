"""MTF fractal directional regime + realign entry research (APTUSDT)."""

from __future__ import annotations

AUDIT_VERSION = "fractal_direction_and_entry_v1"
SYMBOL = "APTUSDT"
MIN_SAMPLE = 30
WEAK_PRICE_ABS = 0.02  # fixed; same as prior fractal work
ROUNDTRIP_FEE_PCT = 0.11  # reference only; no cost optimization

WAVE_DIR = (
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "fractal_cycle_wave_analysis_apt"
)

# Hierarchy labels
REGIME_TFS = ("1M", "1w", "1d")
OPERATIVE_TFS = ("4h", "1h")
TRIGGER_TFS = ("30m", "15m", "5m", "1m")
ALL_JOIN_TFS = ("1M", "1w", "1d", "4h", "1h", "30m", "15m", "5m", "1m")

TF_PREFIX = {
    "1M": "M1",
    "1w": "W1",
    "1d": "D1",
    "4h": "H4",
    "1h": "H1",
    "30m": "M30",
    "15m": "M15",
    "5m": "M5",
    "1m": "M1m",
}

HORIZONS_MIN = (5, 15, 30, 60, 120, 240)
STATES = ("STRONG_BULL", "BULL", "MIXED", "BEAR", "STRONG_BEAR")

WAVE_FEATURE_COLS = [
    "direction",
    "stoch_k_end",
    "stoch_zone_end",
    "stoch_state_end",
    "directional_efficiency",
    "signed_price_move_pct",
    "price_move_pct",
    "rsi_end",
    "rsi_delta",
    "rsi_end_gt_50",
    "rsi_end_lt_50",
    "price_vs_ema20_end",
    "ema9_vs_ema20_end",
    "ema100_end",
    "ema400_end",
    "cci_end",
    "cci_strongest_pos",
    "cci_strongest_neg",
    "end_available_at",
    "start_available_at",
    "inefficient_flag",
]

__all__ = ["AUDIT_VERSION", "SYMBOL", "STATES"]

"""15m wave-failure conditioned on MTF cycle phase (APTUSDT)."""

from __future__ import annotations

AUDIT_VERSION = "fractal_cycle_phase_failure_v1"
SYMBOL = "APTUSDT"
MIN_SAMPLE = 30
VERY_SMALL = 10
WEAK_PRICE_ABS = 0.02

WAVE_DIR = (
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "fractal_cycle_wave_analysis_apt"
)

HORIZONS_MIN = (15, 30, 60, 120, 240)

PHASES = (
    "LOW_UP",
    "MID_UP",
    "HIGH_UP",
    "HIGH_DOWN",
    "MID_DOWN",
    "LOW_DOWN",
)

EARLY_UP = ("LOW_UP", "MID_UP")
LATE_UP = ("HIGH_UP",)
EARLY_DOWN = ("HIGH_DOWN", "MID_DOWN")
LATE_DOWN = ("LOW_DOWN",)

CONTEXT_TFS = ("1d", "4h", "1h")
SOFT_TFS = ("1M", "1w")  # carry only; do not drive decisions
MICRO_TFS = ("5m", "1m")

TF_PREFIX = {
    "1M": "M1",
    "1w": "W1",
    "1d": "D1",
    "4h": "H4",
    "1h": "H1",
    "15m": "M15",
    "5m": "M5",
    "1m": "M1m",
}

WAVE_COLS = [
    "direction",
    "stoch_k_start",
    "stoch_k_end",
    "stoch_delta",
    "stoch_zone_start",
    "stoch_zone_end",
    "directional_efficiency",
    "signed_price_move_pct",
    "price_move_pct",
    "rsi_start",
    "rsi_end",
    "rsi_delta",
    "rsi_end_gt_50",
    "rsi_end_lt_50",
    "price_vs_ema20_end",
    "ema9_vs_ema20_end",
    "ema100_end",
    "ema400_end",
    "inefficient_flag",
    "end_available_at",
    "start_available_at",
]

PHASE_DOC = """
cycle_phase from completed Stoch wave (fixed existing zones LOW/MID/HIGH):
  UP  + zone_end LOW  -> LOW_UP
  UP  + zone_end MID  -> MID_UP
  UP  + zone_end HIGH -> HIGH_UP
  DOWN + zone_end HIGH -> HIGH_DOWN
  DOWN + zone_end MID  -> MID_DOWN
  DOWN + zone_end LOW  -> LOW_DOWN
Optional turning flags (not primary class):
  TURNING_UP:   direction UP  and zone_start=LOW  and zone_end in {MID,HIGH}
  TURNING_DOWN: direction DOWN and zone_start=HIGH and zone_end in {MID,LOW}
No new thresholds.
"""

FAILURE_DOC = """
15m failure episode (one wave = one episode):
  FAILED_UP_WAVE:   direction=UP   and (signed_price_move_pct<=0
                    or directional_efficiency<=0 or inefficient_flag)
                    OR inefficient_up_in_bear after HTF join
  FAILED_DOWN_WAVE: direction=DOWN and (signed<=0 or eff<=0 or inefficient_flag)
                    OR inefficient_down_in_bull after HTF join
decision_time = wave end_available_at
"""

__all__ = ["AUDIT_VERSION", "SYMBOL", "PHASES"]

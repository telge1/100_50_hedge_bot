"""Frozen ALL-WAVE Stoch fade generalization audit (no re-optimization)."""

from __future__ import annotations

from pathlib import Path

AUDIT_VERSION = "fractal_all_wave_fade_generalization_v1"
SOURCE_AUDIT = "fractal_all_wave_fade_v1"
FEE_PCT = 0.11
MIN_SAMPLE = 30
VERY_SMALL = 15
BOOTSTRAP_N = 500
BOOTSTRAP_SEED = 42
PIVOT_STRENGTH = 3  # bars each side; confirmation at center+strength

APT_IS_RESULTS = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/fractal_all_wave_fade_apt"
)
APT_WAVE_DIR = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/fractal_cycle_wave_analysis_apt"
)

TRADING_TFS = ("5m", "15m", "30m", "1h", "4h")

MAIN_HORIZON_BY_TF = {
    "5m": 30,
    "15m": 60,
    "30m": 120,
    "1h": 240,
    "4h": 720,
}

EDGE_DELAYS_BY_TF = {
    "5m": (0, 1, 3, 5, 10),
    "15m": (0, 1, 3, 5, 10, 15),
    "30m": (0, 5, 10, 15, 30),
    "1h": (0, 5, 15, 30, 60),
    "4h": (0, 15, 30, 60, 120),
}

# Frozen from APT IS research window (max entry_time in all_wave_events.csv).
APT_IS_END = "2026-08-08T10:21:00+00:00"
# Minimum OOS span after IS end to claim APT temporal OOS.
APT_OOS_MIN_DAYS = 7
APT_OOS_MIN_WAVES_15M = 30

SYMBOLS = ("APTUSDT_OOS", "DOGEUSDT", "BTCUSDT")

DEFINITIONS_DOC = """
FROZEN DEFINITIONS (from fractal_all_wave_fade_v1 / underlying wave pipeline)
=============================================================================
Runner (IS): scripts/run_fractal_all_wave_fade_apt.py
Package: orderbook_analyse.fractal_all_wave_fade
Wave builder: fractal_cycle_wave_analysis.segment_stoch_waves
Failure mask: fractal_cycle_phase_failure.events.local_failure_mask

Wave Start/End:
  Stoch K/D cross segmentation; wave runs between opposite crosses.
  start_available_at / end_available_at = candle available_at (close) of start/end bars.
  MIN_WAVE_BARS=3.

UP / DOWN:
  direction from starting cross (bullish cross -> UP, bearish -> DOWN).

Fade direction (ALL WAVES, no filter):
  UP wave   -> expected_reversal=DOWN, side=SHORT
  DOWN wave -> expected_reversal=UP,   side=LONG

FAILED / NON_FAILED (comparison only; same local_failure_mask):
  FAILED_UP:   direction=UP   and (signed_price_move_pct<=0 or directional_efficiency<=0
               or inefficient_flag)
  FAILED_DOWN: direction=DOWN and (signed<=0 or eff<=0 or inefficient_flag)
  inefficient_flag: |price_move_pct|<=0.02 and |stoch_delta|>=10
  NON_FAILED: not failed.

Zones HIGH/MID/LOW:
  Existing stoch_zone from K thresholds LOW<=20, HIGH>=80, else MID.
  Endzone = stoch_zone_end; path = stoch_zone_start -> stoch_zone_end.

Efficiency:
  signed_price_move_pct / abs(stoch_delta)
  Quantiles: APT-IS frozen quartile edges per (TF, direction), applied unchanged.

Wave Size:
  signed_price_move_pct (primary); APT-IS frozen quartile edges.

RSI context:
  buckets lt40 / 40_50 / 50_60 / gt60 on rsi_end.
  Hypothesis check: UP+gt60 vs other; DOWN+lt40 vs other.

EMA / trend context:
  EMA_BULL: price_vs_ema20_end=ABOVE and ema9_vs_ema20_end=BULL
  EMA_BEAR: BELOW and BEAR
  else MIXED
  Hypothesis: UP+BULL and DOWN+BEAR stronger fades.

Previous wave:
  Immediate prior wave chronologically.
  CURRENT_STRONGER_THAN_PREVIOUS if opposite prev and cur_eff > prev_eff
  CURRENT_WEAKER_THAN_PREVIOUS if opposite prev and cur_eff < prev_eff

Entry T0:
  confirmation_available_at = end_available_at
  entry = first 1m OPEN with open_time STRICTLY AFTER confirmation
  (searchsorted side=right). No entry on wave-end close.

Delays: exact EDGE_DELAYS_BY_TF maps (minutes added before first-open search).

Forward H (main): 5m->30, 15m->60, 30m->120, 1h->240, 4h->720 minutes on 1m OHLC.
Directional return: positive = fade profit.
Fees: 0.11% roundtrip subtracted for net; no slippage in IS definition.

APT IS window end (frozen): 2026-08-08T10:21:00Z
APT temporal OOS requires data AFTER this timestamp.
"""

__all__ = [
    "AUDIT_VERSION",
    "MAIN_HORIZON_BY_TF",
    "EDGE_DELAYS_BY_TF",
    "DEFINITIONS_DOC",
    "APT_IS_END",
]

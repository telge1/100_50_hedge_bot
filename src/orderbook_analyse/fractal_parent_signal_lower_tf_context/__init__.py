"""Parent 1h/4h Tier-A signal × causal lower-TF Stoch phase context (research)."""

from __future__ import annotations

from pathlib import Path

AUDIT_VERSION = "fractal_parent_signal_lower_tf_context_v1"
FEE_PCT = 0.11
MIN_SAMPLE = 30
VERY_SMALL = 15

EVENTS_PATH = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "fractal_wave_fade_trend_filter_generalization/events_with_trend.csv"
)
WAVE_CACHE_ROOT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "fractal_all_wave_fade_generalization/wave_cache"
)
STAGING_1M_DIR = Path(
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/data_htf_candle_staging/futures"
)

SYMBOLS = ("DOGEUSDT", "BTCUSDT")
PARENT_TFS = ("1h", "4h")

# Lower TFs by parent (excluding parent itself)
LOWER_TFS = {
    "1h": ("30m", "15m", "5m", "1m"),
    "4h": ("1h", "30m", "15m", "5m", "1m"),
}

# Counts use these (exclude 1m for exhausted/ready multi-count as specified)
COUNT_TFS = {
    "1h": ("30m", "15m", "5m"),
    "4h": ("1h", "30m", "15m", "5m"),
}

HORIZONS = {
    "1h": (240, 480),
    "4h": (720, 1440),
}

FIXED_TPSL = {
    "1h": ((2.0, 1.5),),
    "4h": ((4.0, 2.0), (6.0, 3.0)),
}

REACH_LEVELS = (1.0, 2.0, 4.0)

REL_CONTEXT_DOC = """
Relative context vs parent fade side (priority to avoid overlap):
SHORT:
  FAVORABLE_EARLY if zone==HIGH
  FAVORABLE_MID   if phase==MID_DOWN
  LATE            if zone==LOW
  COUNTER         if direction==UP (and not already LATE/EARLY/MID)
  OTHER           else
LONG (mirror):
  FAVORABLE_EARLY if zone==LOW
  FAVORABLE_MID   if phase==MID_UP
  LATE            if zone==HIGH
  COUNTER         if direction==DOWN (and not already LATE/EARLY/MID)
  OTHER           else
Raw phase (LOW_UP..LOW_DOWN) always retained separately.
Zone-only HIGH/MID/LOW used for extended hypothesis.
"""

METHOD_DOC = """
Frozen Tier-A parent wave-end fade (1h/4h). T0 entry unchanged.
Lower-TF context = last completed Stoch wave with end_available_at <= confirmation_available_at.
Zones/phases from frozen definitions (K LOW<=20 / HIGH>=80 via existing wave fields).
No new thresholds, no hard filters, no TP/SL search.
Fixed research TPSL only: 1h TP2/SL1.5; 4h TP4/SL2 and TP6/SL3.
""" + REL_CONTEXT_DOC

__all__ = ["AUDIT_VERSION", "LOWER_TFS", "HORIZONS", "FIXED_TPSL"]

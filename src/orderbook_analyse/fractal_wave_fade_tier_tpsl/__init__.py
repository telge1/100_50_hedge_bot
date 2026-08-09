"""Tiered wave-fade TP/SL generalization (frozen defs, DOGE/BTC)."""

from __future__ import annotations

from pathlib import Path

AUDIT_VERSION = "fractal_wave_fade_tier_tpsl_generalization_v1"
FEE_PCT = 0.11
MIN_SAMPLE = 30
VERY_SMALL = 15

EVENTS_PATH = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "fractal_wave_fade_trend_filter_generalization/events_with_trend.csv"
)

SIGNAL_TFS = ("15m", "30m", "1h", "4h")
SYMBOLS = ("DOGEUSDT", "BTCUSDT")

MAX_HOLD_MIN = {
    "15m": 12 * 60,  # 12h
    "30m": 24 * 60,  # 24h
    "1h": 48 * 60,  # 48h
    "4h": 7 * 24 * 60,  # 7d
}

# Short-horizon sensitivity (prior main horizons)
SHORT_H_MIN = {
    "15m": 60,
    "30m": 120,
    "1h": 240,
    "4h": 720,
}

TP_GRID = (0.25, 0.50, 0.75, 1.00, 1.50, 2.00, 3.00, 4.00, 5.00, 6.00)
SL_GRID = (0.25, 0.50, 0.75, 1.00, 1.50, 2.00, 3.00, 4.00)

REFERENCE_COMBOS = (
    (0.50, 1.00),
    (0.75, 1.00),
    (1.00, 1.00),
    (1.00, 1.50),
    (1.50, 1.00),
    (1.50, 1.50),
    (2.00, 1.00),
    (2.00, 1.50),
    (2.00, 2.00),
    (3.00, 1.50),
    (3.00, 2.00),
    (4.00, 2.00),
    (4.00, 3.00),
    (5.00, 2.00),
    (6.00, 3.00),
)

REACH_LEVELS = (0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0)
LARGE_TARGETS = (1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
LARGE_ADVERSE = (0.5, 1.0, 1.5, 2.0)

TIERS = ("ALL", "A", "B", "C", "D", "MIXED")

METHOD_DOC = """
Frozen signal: ALL-WAVE fade T0 (UP->SHORT, DOWN->LONG), entry first 1m open
strictly after end_available_at.
Tiers from frozen H4 trend + APT-IS efficiency quartiles:
  A: TREND_ALIGNED + Q4
  B: TREND_ALIGNED + Q1-Q3
  C: COUNTERTREND + Q4
  D: COUNTERTREND + Q1-Q3
  MIXED: ema MIXED (reported separately)
TP/SL grids fixed; max-hold TF-specific; fees 0.11%; SL_FIRST primary.
No new filters/thresholds. Research candidates only.
"""

__all__ = ["AUDIT_VERSION", "TP_GRID", "SL_GRID", "REFERENCE_COMBOS", "MAX_HOLD_MIN"]

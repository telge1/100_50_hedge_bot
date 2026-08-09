"""DB-sourced parent Tier-A × a-priori lower-TF quality rank (research)."""

from __future__ import annotations

from pathlib import Path

AUDIT_VERSION = "fractal_parent_lower_tf_quality_db_v1"
FEE_PCT = 0.11
MIN_SAMPLE = 30
VERY_SMALL = 15

ENV_FILE = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/"
    "research/regime_scanner/.env.regime_db"
)

SYMBOLS = ("DOGEUSDT", "BTCUSDT")
PARENT_TFS = ("1h", "4h")
APT_IS_END = "2026-08-08T10:21:00+00:00"

LOWER_TFS = {
    "1h": ("30m", "15m", "5m"),
    "4h": ("1h", "30m", "15m", "5m"),
}

HORIZONS = {
    "1h": (240, 480),
    "4h": (720, 1440),
}

FIXED_TPSL = {
    "1h": ((2.0, 1.5), (3.0, 2.0)),
    "4h": ((4.0, 2.0), (6.0, 3.0)),
}

MAX_HOLD_MIN = {
    "1h": 72 * 60,
    "4h": 10 * 24 * 60,
}

QUALITY_CLASSES = ("A_PLUS_TIMING", "A_TIMING", "A_MINUS_TIMING")

# Fixed research sizing weights (not optimized)
SIZE_WEIGHTS = {
    "A_PLUS_TIMING": 1.0,
    "A_TIMING": 0.75,
    "A_MINUS_TIMING": 0.5,
}

QUALITY_RULE_DOC = """
A-priori lower-TF quality classes (fixed BEFORE outcome evaluation).

Parent remains Tier A always. Lower TFs only assign a timing/quality label.
No entry delay: all classes trade at frozen T0.

Lower TFs (count set):
  1h parent → 30m, 15m, 5m
  4h parent → 1h, 30m, 15m, 5m

Per lower TF (last completed wave with end_available_at <= confirmation_available_at):
  zone = stoch_zone_end ∈ {LOW, MID, HIGH}
  phase = direction × zone (LOW_UP … LOW_DOWN)

SHORT parent:
  exhausted_i = 1 if zone==LOW else 0
  favorable_i = 1 if phase ∈ {HIGH_UP, HIGH_DOWN, MID_DOWN} or zone==HIGH else 0
  exhausted_count = sum exhausted_i
  favorable_count = sum favorable_i

  A_PLUS_TIMING  if exhausted_count == 0 AND favorable_count >= 2
  A_MINUS_TIMING if exhausted_count >= 2
  A_TIMING       otherwise

LONG parent (mirror):
  exhausted_i = 1 if zone==HIGH else 0
  favorable_i = 1 if phase ∈ {LOW_DOWN, LOW_UP, MID_UP} or zone==LOW else 0
  same count thresholds for A+/A/A-

1m is NOT used in quality classification (optional diagnostic only; omitted here).
"""

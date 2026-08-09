"""Trend-filter + Q4 efficiency on frozen ALL-WAVE fade (no new logic)."""

from __future__ import annotations

from pathlib import Path

AUDIT_VERSION = "fractal_wave_fade_trend_filter_generalization_v1"
SOURCE_GENERALIZATION = "fractal_all_wave_fade_generalization_v1"
FEE_PCT = 0.11
MIN_SAMPLE = 30
VERY_SMALL = 15

GEN_DIR = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "fractal_all_wave_fade_generalization"
)
WAVE_CACHE = GEN_DIR / "wave_cache"

SIGNAL_TFS = ("15m", "30m", "1h", "4h")
MAIN_HORIZON_BY_TF = {
    "15m": 60,
    "30m": 120,
    "1h": 240,
    "4h": 720,
}

# Frozen H4 / DEFINITIONS.md trend-aligned mapping (NOT section-5 inverted wording):
# UP wave + EMA_BULL  -> SHORT fade is TREND_ALIGNED (with-trend wave exhaustion)
# DOWN wave + EMA_BEAR -> LONG fade is TREND_ALIGNED
# UP + EMA_BEAR / DOWN + EMA_BULL -> COUNTERTREND
# EMA MIXED -> MIXED
TREND_DOC = """
SOURCE OF TRUTH: results/fractal_all_wave_fade_generalization/DEFINITIONS.md (H4)

EMA / trend context (same-TF wave-end labels; no HTF voting, no new combo):
  EMA_BULL: price_vs_ema20_end=ABOVE AND ema9_vs_ema20_end=BULL
  EMA_BEAR: price_vs_ema20_end=BELOW AND ema9_vs_ema20_end=BEAR
  else MIXED

TREND_ALIGNED (matches H4 'trend-aligned stronger fades'):
  SHORT: direction=UP   AND ema_context=EMA_BULL
  LONG:  direction=DOWN AND ema_context=EMA_BEAR

COUNTERTREND:
  SHORT: direction=UP   AND ema_context=EMA_BEAR
  LONG:  direction=DOWN AND ema_context=EMA_BULL

MIXED:
  ema_context=MIXED (or unknown)

NOTE: Prompt §5 wording inverted relative to H4; this package follows H4/DEFINITIONS.
Efficiency Q1..Q4: APT-IS frozen cuts from generalization annotate.py.
"""

__all__ = ["AUDIT_VERSION", "SIGNAL_TFS", "MAIN_HORIZON_BY_TF", "TREND_DOC"]

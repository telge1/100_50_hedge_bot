"""Hierarchical Fractal Cycle wave + price-efficiency research (read-only)."""

from __future__ import annotations

AUDIT_VERSION = "fractal_cycle_wave_analysis_v1"

SYMBOL_PRIMARY = "APTUSDT"
EXCHANGE = "bybit"

# Parent cycle context vs subordinate waves (research labels only).
PARENT_TFS = ("1M", "1w", "1d")
WAVE_TFS = ("4h", "1h", "30m", "15m", "5m", "1m")
ALL_TFS = ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M")

# Indicator params (fixed; no optimization).
RSI_LENGTH = 14
STOCH_RSI_LENGTH = 14
STOCH_K_SMOOTH = 3
STOCH_D_SMOOTH = 3
STOCH_LOW_K = 20.0
STOCH_HIGH_K = 80.0
CCI_LENGTH = 20
EMA_SPANS = (9, 20, 100, 400)

# Wave segmentation: K/D cross runs; drop tiny noise waves.
MIN_WAVE_BARS = 3
# Inefficient counter-wave: |price_move_pct| below this while |ΔK| is meaningful.
INEFFICIENT_ABS_PRICE_PCT = 0.02
MIN_ABS_STOCH_DELTA = 10.0

# Decision thresholds (documented in REPORT).
VISIBLE_MIN_TFS_WITH_SIGN = 5  # of 9 TFs: UP mean>0 and DOWN mean<0
VISIBLE_MIN_ABS_MEAN_MOVE = 0.05  # % median across signed TFs
WEAK_MIN_TFS_WITH_SIGN = 3

__all__ = [
    "AUDIT_VERSION",
    "SYMBOL_PRIMARY",
    "ALL_TFS",
    "PARENT_TFS",
    "WAVE_TFS",
]

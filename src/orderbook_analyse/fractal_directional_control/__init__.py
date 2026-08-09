"""Directional control / CCI turn analysis from existing fractal wave CSVs."""

from __future__ import annotations

AUDIT_VERSION = "fractal_directional_control_v1"
SYMBOL = "APTUSDT"

REGIME_TFS = ("1d", "1w", "1M")
CONTEXT_TFS = ("4h", "1h")
TRIGGER_TFS = ("15m", "5m", "1m")

# Fixed logical groups — no threshold search.
WEAK_PRICE_ABS = 0.02  # "kaum" move
CCI_STRONG = 150.0  # fixed strong extreme for combo tests
CCI_BUCKETS = (
    ("lt100", 0.0, 100.0),
    ("100_150", 100.0, 150.0),
    ("150_200", 150.0, 200.0),
    ("200_300", 200.0, 300.0),
    ("gt300", 300.0, float("inf")),
)
MIN_SAMPLE_MARK = 30

__all__ = ["AUDIT_VERSION", "SYMBOL", "TRIGGER_TFS", "CONTEXT_TFS", "REGIME_TFS"]

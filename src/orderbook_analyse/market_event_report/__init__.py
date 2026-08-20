"""Causal market-event short report (research diagnostic, not a trading signal)."""

from .classify import CLASSIFICATION_LABELS, classify_event
from .metrics import (
    FUTURE_HORIZONS_M,
    PRE_WINDOWS_M,
    mfe_mae_both_sides,
    path_window_bars,
    pre_post_price_metrics,
)

__all__ = [
    "CLASSIFICATION_LABELS",
    "FUTURE_HORIZONS_M",
    "PRE_WINDOWS_M",
    "classify_event",
    "mfe_mae_both_sides",
    "path_window_bars",
    "pre_post_price_metrics",
]

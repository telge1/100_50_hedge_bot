"""EMA dual-cross + multi-source gate research pipeline (no live orders)."""

from .config import EMA_DUAL_CROSS_DEFAULTS, POLICY_VERSION
from .pipeline import STRATEGY_ID, STRATEGY_VERSION, run_ema_dual_cross_on_candles

__all__ = [
    "EMA_DUAL_CROSS_DEFAULTS",
    "POLICY_VERSION",
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "run_ema_dual_cross_on_candles",
]

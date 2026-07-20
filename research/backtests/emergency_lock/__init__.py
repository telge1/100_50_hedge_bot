"""Isolated Emergency-Lock / Short-Unlock stress backtester (Phase A+).

This package must not import the original hedge strategy engine,
historical_backtest, or recovery/refill/stuck/addon/DCOS modules.
"""

from .config import EmergencyLockRecoveryConfig

__all__ = ["EmergencyLockRecoveryConfig"]

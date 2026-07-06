from __future__ import annotations

"""
Backtest-only recovery bot scaffolding.

Phase 1: configuration, state tracking, and pure calculations.

This package is intentionally independent of the live strategy and existing
backtest features. Integration with the simulator/backtest loop will be
added in later phases.
"""

__all__ = [
    "RecoveryBotConfig",
    "RecoveryState",
    "RecoveryBotTracker",
]

from .config import RecoveryBotConfig  # noqa: E402
from .state import RecoveryBotTracker, RecoveryState  # noqa: E402


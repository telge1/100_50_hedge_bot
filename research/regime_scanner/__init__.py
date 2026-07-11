"""Backtest-only market regime scanner (research package).

Causal candle loading, indicators, point audits, RegimeSnapshot, thin
SetupActivation, and Phase-2 PriceActionConfirmation. No live trading.
"""

from .config import RegimeScannerConfig, default_regime_scanner_config
from .price_action import (
    evaluate_price_action_confirmation,
    initialize_price_action_state,
    update_price_action_state,
)
from .regime_snapshot import (
    build_regime_snapshot,
    build_regime_snapshot_from_point_audit,
    evaluate_setup_activation,
    snapshot_and_setup_from_point_audit,
)

__all__ = [
    "RegimeScannerConfig",
    "default_regime_scanner_config",
    "build_regime_snapshot",
    "build_regime_snapshot_from_point_audit",
    "evaluate_setup_activation",
    "snapshot_and_setup_from_point_audit",
    "initialize_price_action_state",
    "update_price_action_state",
    "evaluate_price_action_confirmation",
]

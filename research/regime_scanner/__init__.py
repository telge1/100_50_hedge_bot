"""Backtest-only market regime scanner (research package).

Causal candle loading, indicators, point audits, RegimeSnapshot, and thin
SetupActivation (Phase 1 of Setup→PA→Momentum→Entry). No live trading.
"""

from .config import RegimeScannerConfig, default_regime_scanner_config
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
]

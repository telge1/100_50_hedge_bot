"""Backtest-only market regime scanner (research package).

Phase 1-3 provides causal candle loading, Wilder/EMA indicators, and a
point-in-time audit CLI. No live trading decisions or strategy changes.
"""

from .config import RegimeScannerConfig, default_regime_scanner_config

__all__ = [
    "RegimeScannerConfig",
    "default_regime_scanner_config",
]

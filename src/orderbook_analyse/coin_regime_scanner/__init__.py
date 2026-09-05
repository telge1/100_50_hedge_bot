"""COIN_REGIME_SCANNER_V1 — research-only causal multi-coin regime snapshot.

No live trading, no dashboard, no ClickHouse writes, no collector control.
"""

from .classify import build_coin_regime
from .config import SCANNER_VERSION, default_warmup_hours
from .runner import run_scanner

__all__ = [
    "SCANNER_VERSION",
    "default_warmup_hours",
    "build_coin_regime",
    "run_scanner",
]

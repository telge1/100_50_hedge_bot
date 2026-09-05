"""Causal candidate detector: Regime → EMA-Zone → Microstructure → Candidate State.

Research-only. No entry/exit/TP/SL/size/portfolio/PnL execution.
"""

from __future__ import annotations

FORMAT_VERSION = "ema_zone_microstructure_confirmation/v1"
PLUGIN_ID = "ema_zone_microstructure_confirmation"
STRATEGY_YAML = "strategies/strategy_lab/ema_zone_microstructure_confirmation_v1.yaml"
OUT_SUBDIR = "btc_manual_windows_v1"

__all__ = [
    "FORMAT_VERSION",
    "PLUGIN_ID",
    "STRATEGY_YAML",
    "OUT_SUBDIR",
]

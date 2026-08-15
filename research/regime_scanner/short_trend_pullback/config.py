"""Frozen config for short_trend_pullback_v1 (no free optimization)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

STRATEGY_NAME = "short_trend_pullback"
STRATEGY_VERSION = "short_trend_pullback_v1"
FEATURE_VERSION = "stp_features_v1"
OUTCOME_VERSION = "tp3_sl2_h192_cost020_v1"
SCANNER_NAME = "short_trend_pullback_v1"

CONTEXTS = ("B1", "B2", "B3")
TRIGGERS = ("E1", "E2", "E3", "E4")

# Frozen economic / structural bounds (not grid-searched).
MIN_IMPULSE_ATR = 0.50
MIN_IMPULSE_BARS = 2
MAX_IMPULSE_BARS = 32
MAX_PULLBACK_BARS = 16
MAX_RETRACEMENT = 0.786
MIN_PULLBACK_BARS = 1
SLOPE_LOOKBACK = 3
# Price "persistently above EMA200" → recent closes mostly above (causal window).
EMA200_ABOVE_LOOKBACK = 8
EMA200_ABOVE_SHARE_MAX = 0.50  # >50% of last N closes above EMA200 → not B1

TP_PCT = 3.0
SL_PCT = -2.0
HORIZON_BARS = 192
COST_PCT = 0.20

MFE_HORIZONS = (4, 8, 16, 24, 48, 96, 192)
FIRST_TOUCH_LEVELS = (0.25, 0.50, 1.00, 2.00, 3.00, -0.25, -0.50, -1.00, -2.00)

DEFAULT_SYMBOLS = (
    "APTUSDT",
    "ENAUSDT",
    "ARBUSDT",
    "OPUSDT",
    "SUIUSDT",
    "SEIUSDT",
    "ADAUSDT",
    "XRPUSDT",
    "SOLUSDT",
    "DOGEUSDT",
    "LINKUSDT",
    "AVAXUSDT",
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
)

A6_PARENT_LABEL = "multicoin_a6_signal_store_20260722"


@dataclass(frozen=True)
class STPConfig:
    strategy_name: str = STRATEGY_NAME
    strategy_version: str = STRATEGY_VERSION
    min_impulse_atr: float = MIN_IMPULSE_ATR
    min_impulse_bars: int = MIN_IMPULSE_BARS
    max_impulse_bars: int = MAX_IMPULSE_BARS
    max_pullback_bars: int = MAX_PULLBACK_BARS
    max_retracement: float = MAX_RETRACEMENT
    min_pullback_bars: int = MIN_PULLBACK_BARS
    slope_lookback: int = SLOPE_LOOKBACK
    ema200_above_lookback: int = EMA200_ABOVE_LOOKBACK
    ema200_above_share_max: float = EMA200_ABOVE_SHARE_MAX
    tp_pct: float = TP_PCT
    sl_pct: float = SL_PCT
    horizon_bars: int = HORIZON_BARS
    cost_pct: float = COST_PCT

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def config_hash(self) -> str:
        blob = json.dumps(self.to_dict(), sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()


def default_config() -> STPConfig:
    return STPConfig()


def variant_id(context: str, trigger: str) -> str:
    return f"{STRATEGY_VERSION}__{context}__{trigger}"

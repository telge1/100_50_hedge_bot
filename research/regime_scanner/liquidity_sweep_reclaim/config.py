"""Frozen config for liquidity_sweep_reclaim_v1 (no free optimization)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

STRATEGY_NAME = "liquidity_sweep_reclaim"
STRATEGY_VERSION = "liquidity_sweep_reclaim_v1"
FEATURE_VERSION = "lsr_features_v1"
SCANNER_NAME = "liquidity_sweep_reclaim_v1"

# L3 unavailable — documented in reuse_analysis.md
LEVEL_FAMILIES = ("L1", "L2")
LEVEL_FAMILIES_REQUESTED = ("L1", "L2", "L3")
PENETRATIONS = ("P1", "P2", "P3")
RECLAIMS = ("R1", "R2", "R3")

CLUSTER_TOLERANCE_ATR = 0.20  # frozen; unused while L3 unavailable
MAX_PENETRATION_ATR = 1.00
P2_MIN_ATR = 0.10
P3_MIN_ATR = 0.25
MIN_RANGE_AGE_BARS = 3
MIN_PROTECTED_AGE_BARS = 1
C31_VARIANT = "balanced"

MFE_HORIZONS = (1, 2, 4, 8, 16, 24, 48, 96, 192)
FIRST_TOUCH_FAVORABLE = (0.25, 0.50, 0.75, 1.00, 2.00, 3.00)
FIRST_TOUCH_ADVERSE = (-0.25, -0.50, -0.75, -1.00, -2.00)

# exit_id -> (tp_pct, sl_pct_magnitude, horizon, cost)
EXIT_BENCHMARKS: dict[str, tuple[float, float, int, float]] = {
    "X1": (0.50, 0.50, 48, 0.20),
    "X2": (0.75, 0.75, 96, 0.20),
    "X3": (1.00, 1.00, 192, 0.20),
    "X4": (1.00, 0.75, 192, 0.20),
    "X5": (3.00, 2.00, 192, 0.20),
}
COST_STRESS_PCT = 0.25

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
STP_RESULTS_DIR = "research/regime_scanner/results/short_trend_pullback_v1_20260722"
STP_VARIANT = "B2xE1"

MAJORS = frozenset({"BTCUSDT", "ETHUSDT", "BNBUSDT"})
TOP3 = frozenset({"BTCUSDT", "ETHUSDT", "SOLUSDT"})


@dataclass(frozen=True)
class LSRConfig:
    strategy_name: str = STRATEGY_NAME
    strategy_version: str = STRATEGY_VERSION
    max_penetration_atr: float = MAX_PENETRATION_ATR
    p2_min_atr: float = P2_MIN_ATR
    p3_min_atr: float = P3_MIN_ATR
    min_range_age_bars: int = MIN_RANGE_AGE_BARS
    min_protected_age_bars: int = MIN_PROTECTED_AGE_BARS
    c31_variant: str = C31_VARIANT
    cluster_tolerance_atr: float = CLUSTER_TOLERANCE_ATR

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def config_hash(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()


def default_config() -> LSRConfig:
    return LSRConfig()


def variant_id(level_family: str, penetration: str, reclaim: str) -> str:
    return f"{level_family}x{penetration}x{reclaim}"


def all_variants(
    level_families: tuple[str, ...] = LEVEL_FAMILIES,
    penetrations: tuple[str, ...] = PENETRATIONS,
    reclaims: tuple[str, ...] = RECLAIMS,
) -> list[str]:
    out: list[str] = []
    for lf in level_families:
        if lf == "L3":
            continue
        for p in penetrations:
            for r in reclaims:
                out.append(variant_id(lf, p, r))
    return out


def penetration_min_atr(p_class: str, cfg: LSRConfig | None = None) -> float:
    c = cfg or default_config()
    if p_class == "P1":
        return 0.0
    if p_class == "P2":
        return float(c.p2_min_atr)
    if p_class == "P3":
        return float(c.p3_min_atr)
    raise ValueError(f"unknown penetration class: {p_class}")

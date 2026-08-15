"""Frozen V1 config for Level-Context × Orderflow Absorption audit."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from research.regime_scanner.orderflow_absorption.config import (
    ATR_PERIOD,
    BAR_SECONDS,
    F1_ABS,
    IMPORT_VERSION_DEFAULT,
    NORMAL_PROGRESS_ABS,
    UNAVAILABLE_SYMBOLS,
    WEAK_PROGRESS_ABS,
)

AUDIT_NAME = "orderflow_absorption_level"
AUDIT_VERSION = "orderflow_absorption_level_v1"

DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "APTUSDT")
DEFAULT_PATTERNS = ("A4", "A2", "A1")
DEFAULT_FLOW_RULES = ("F1",)
DEFAULT_LOOKBACKS = (24,)
DEFAULT_LEVEL_TYPES = ("protected", "external_swing")
DEFAULT_CONFIRMATIONS = ("R0", "R1", "R2")
DEFAULT_HORIZONS = (6, 12)
DEFAULT_MOVE_THRESHOLDS = (0.0025, 0.005)

MAX_DISTANCE_ATR = 0.50
CONFLUENCE_ATR = 0.25
EVENT_COOLDOWN_BARS = 6
PIVOT_LEFT = 3
PIVOT_RIGHT = 3
PROTECTED_VARIANT = "protected_medium"

# Distance buckets (ATR): exclusive upper bounds except touch includes 0.
BUCKET_TOUCH = (0.0, 0.10)
BUCKET_VERY_NEAR = (0.10, 0.25)
BUCKET_NEAR = (0.25, 0.50)

LEVEL_PRIORITY = {"protected": 0, "external_swing": 1}

MIN_EVENTS_STRONG = 100
MIN_EVENTS_ALT = 50
MIN_COINS_ALT = 2
MAX_COIN_SHARE = 0.70
MIN_D_FAV_FIRST_PP = 3.0

PRIMARY_HORIZON = 6
PRIMARY_THRESHOLD = 0.0025

IMPORTED_ABSORPTION = (
    "research.regime_scanner.orderflow_absorption.features.compute_feature_rows",
    "research.regime_scanner.orderflow_absorption.features.enrich_frame",
    "research.regime_scanner.orderflow_absorption.patterns.assignment_rows",
    "research.regime_scanner.orderflow_absorption.outcomes.forward_outcome_at",
    "research.regime_scanner.liquidation_exhaustion.loader.load_joined_5m",
)
IMPORTED_LEVELS = (
    "research.regime_scanner.swings.find_confirmed_pivots",
    "research.regime_scanner.swings.filter_pivots_as_of",
    "research.regime_scanner.market_structure_c3_4b.step_protected_structure_state",
    "research.regime_scanner.market_structure_c3_4b.ProtectedStructureConfig",
    "research.regime_scanner.market_structure_c3_4b.ProtectedRuntime",
    "research.regime_scanner.liquidity_sweep_reclaim.sweep.measure_sweep",
    "research.regime_scanner.liquidity_sweep_reclaim.reclaim.r1_same_candle",
    "research.regime_scanner.liquidity_sweep_reclaim.reclaim.reclaim_close",
)

NEW_ADAPTERS = (
    "levels_build.build_level_inventory",
    "level_assign.assign_levels_to_anchors",
    "events.build_absorption_level_events",
    "confirmations.build_confirmation_events",
    "controls.build_control_assignments",
    "outcomes_level.compute_event_outcomes",
)


def distance_bucket(distance_atr: float | None, *, max_distance_atr: float = MAX_DISTANCE_ATR) -> str:
    if distance_atr is None or distance_atr != distance_atr:
        return "no_level"
    d = float(distance_atr)
    if d < 0:
        return "no_level"
    if d <= BUCKET_TOUCH[1]:
        return "touch"
    if d <= BUCKET_VERY_NEAR[1]:
        return "very_near"
    if d <= max_distance_atr:
        return "near"
    return "far"


def thr_label(thr: float) -> str:
    pct = thr * 100.0
    return f"{pct:.2f}".replace(".", "_") + "pct"


@dataclass(frozen=True)
class LevelAbsorptionConfig:
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS
    patterns: tuple[str, ...] = DEFAULT_PATTERNS
    flow_rules: tuple[str, ...] = DEFAULT_FLOW_RULES
    lookbacks: tuple[int, ...] = DEFAULT_LOOKBACKS
    level_types: tuple[str, ...] = DEFAULT_LEVEL_TYPES
    confirmations: tuple[str, ...] = DEFAULT_CONFIRMATIONS
    horizons: tuple[int, ...] = DEFAULT_HORIZONS
    move_thresholds: tuple[float, ...] = DEFAULT_MOVE_THRESHOLDS
    max_distance_atr: float = MAX_DISTANCE_ATR
    confluence_atr: float = CONFLUENCE_ATR
    event_cooldown_bars: int = EVENT_COOLDOWN_BARS
    pivot_left: int = PIVOT_LEFT
    pivot_right: int = PIVOT_RIGHT
    protected_variant: str = PROTECTED_VARIANT
    import_version: str = IMPORT_VERSION_DEFAULT
    f1_abs: float = F1_ABS
    normal_progress_abs: float = NORMAL_PROGRESS_ABS
    weak_progress_abs: float = WEAK_PROGRESS_ABS
    primary_horizon: int = PRIMARY_HORIZON
    primary_threshold: float = PRIMARY_THRESHOLD
    min_events_strong: int = MIN_EVENTS_STRONG
    min_events_alt: int = MIN_EVENTS_ALT
    min_coins_alt: int = MIN_COINS_ALT
    max_coin_share: float = MAX_COIN_SHARE
    min_d_fav_first_pp: float = MIN_D_FAV_FIRST_PP
    bar_seconds: int = BAR_SECONDS
    atr_period: int = ATR_PERIOD

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def config_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True, default=str).encode()
        ).hexdigest()


def default_config() -> LevelAbsorptionConfig:
    return LevelAbsorptionConfig()


__all__ = [
    "AUDIT_NAME",
    "AUDIT_VERSION",
    "UNAVAILABLE_SYMBOLS",
    "LevelAbsorptionConfig",
    "default_config",
    "distance_bucket",
    "thr_label",
    "LEVEL_PRIORITY",
    "IMPORTED_ABSORPTION",
    "IMPORTED_LEVELS",
    "NEW_ADAPTERS",
]

"""Central frozen config for HTF pivot level preview (Python + Pine docs)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

AUDIT_NAME = "htf_pivot_level_preview"
AUDIT_VERSION = "htf_pivot_level_preview_v1"
IMPORT_VERSION_DEFAULT = "derivatives_5m_v1"

# Primary pivot families (minutes, left, right)
HTF_PIVOT_SPECS: dict[str, dict[str, int]] = {
    "5m": {"minutes": 5, "left": 3, "right": 3},
    "15m": {"minutes": 15, "left": 3, "right": 3},
    "1h": {"minutes": 60, "left": 3, "right": 3},
    "4h": {"minutes": 240, "left": 4, "right": 4},
    "12h": {"minutes": 720, "left": 5, "right": 5},
    "1D": {"minutes": 1440, "left": 5, "right": 5},
}

SOURCE_HTF_5M = "htf_pivot_5m"
SOURCE_HTF_15M = "htf_pivot_15m"
SOURCE_HTF_1H = "htf_pivot_1h"
SOURCE_HTF_4H = "htf_pivot_4h"
SOURCE_HTF_12H = "htf_pivot_12h"
SOURCE_HTF_1D = "htf_pivot_1d"
SOURCE_EXTERNAL_SWING = "external_swing"
SOURCE_PROTECTED = "protected"

ALL_SOURCE_TYPES = (
    SOURCE_HTF_5M,
    SOURCE_HTF_15M,
    SOURCE_HTF_1H,
    SOURCE_HTF_4H,
    SOURCE_HTF_12H,
    SOURCE_HTF_1D,
    SOURCE_EXTERNAL_SWING,
    SOURCE_PROTECTED,
)

INVALIDATION_CLOSE_BREAK_ONLY = "close_break_only"
INVALIDATION_REPLACEMENT_ONLY = "replacement_only"
INVALIDATION_BOTH = "close_break_or_replacement"

# Named lifecycle presets for HTF chart review (map onto invalidation_mode).
LIFECYCLE_REPLACEMENT = "replacement"  # close_break_or_replacement
LIFECYCLE_PERSISTENT = "persistent"  # close_break only; no auto-replacement

# Preview pivot families (5m/15m/1h densify near-price; 4h/12h/1D stay core HTF).
HTF_SOURCE_TYPES = frozenset(
    {
        SOURCE_HTF_5M,
        SOURCE_HTF_15M,
        SOURCE_HTF_1H,
        SOURCE_HTF_4H,
        SOURCE_HTF_12H,
        SOURCE_HTF_1D,
    }
)
CORE_HTF_SOURCE_TYPES = frozenset({SOURCE_HTF_4H, SOURCE_HTF_12H, SOURCE_HTF_1D})
DENSE_HTF_SOURCE_TYPES = frozenset({SOURCE_HTF_5M, SOURCE_HTF_15M, SOURCE_HTF_1H})
# Timeframes that use ltf_lookback_days for pivot detection only.
LTF_LOOKBACK_TIMEFRAMES = frozenset({"5m", "15m", "1h"})

TOUCH_WICK = "wick_touch"
TOUCH_CLOSE_DISTANCE = "close_distance"
TOUCH_ATR_ZONE = "atr_zone"

DEFAULT_INVALIDATION_MODE = INVALIDATION_BOTH
DEFAULT_TOUCH_MODE = TOUCH_WICK
DEFAULT_TICK_TOLERANCE = 0.0  # absolute price units; 0 = exact wick touch
DEFAULT_MAX_ACTIVE_LEVELS = 40
DEFAULT_MAX_HISTORICAL_LEVELS = 120
PINE_MAX_LINES = 500
PINE_MAX_LABELS = 500


def invalidation_mode_for_lifecycle(lifecycle: str) -> str:
    if lifecycle == LIFECYCLE_REPLACEMENT:
        return INVALIDATION_BOTH
    if lifecycle == LIFECYCLE_PERSISTENT:
        return INVALIDATION_CLOSE_BREAK_ONLY
    raise ValueError(f"unknown lifecycle mode: {lifecycle}")


def is_htf_source(source_type: object) -> bool:
    return str(source_type) in HTF_SOURCE_TYPES

# External swing defaults (5m chart pivots) — match absorption V1
EXTERNAL_PIVOT_LEFT = 3
EXTERNAL_PIVOT_RIGHT = 3
PROTECTED_VARIANT = "protected_medium"


@dataclass(frozen=True)
class HtfPivotPreviewConfig:
    symbols: tuple[str, ...] = ("APTUSDT",)
    import_version: str = IMPORT_VERSION_DEFAULT
    htf_timeframes: tuple[str, ...] = ("5m", "15m", "1h", "4h", "12h", "1D")
    include_external_swing: bool = False
    include_protected: bool = False
    invalidation_mode: str = DEFAULT_INVALIDATION_MODE
    lifecycle_mode: str = LIFECYCLE_REPLACEMENT
    htf_only: bool = True
    # When True (HTF review default): embed every core HTF level; dense TFs
    # (5m/15m/1h) may be trimmed to pine_max_lines preferring active + nearest.
    embed_all_htf_levels: bool = True
    # Limit 5m/15m/1h pivot detection to recent history (full OHLCV for lifecycle).
    ltf_lookback_days: int = 21
    touch_mode: str = DEFAULT_TOUCH_MODE
    tick_tolerance: float = DEFAULT_TICK_TOLERANCE
    touch_atr_mult: float = 0.10
    max_active_levels: int = DEFAULT_MAX_ACTIVE_LEVELS
    max_historical_levels: int = DEFAULT_MAX_HISTORICAL_LEVELS
    external_pivot_left: int = EXTERNAL_PIVOT_LEFT
    external_pivot_right: int = EXTERNAL_PIVOT_RIGHT
    protected_variant: str = PROTECTED_VARIANT
    # Pine display caps (visual only; does not alter scanner inventory)
    pine_max_lines: int = PINE_MAX_LINES
    pine_max_labels: int = PINE_MAX_LABELS
    pine_max_visible_levels: int = PINE_MAX_LINES

    def htf_spec(self, tf: str) -> dict[str, int]:
        if tf not in HTF_PIVOT_SPECS:
            raise ValueError(f"unknown HTF timeframe: {tf}")
        return dict(HTF_PIVOT_SPECS[tf])

    def source_type_for_tf(self, tf: str) -> str:
        mapping = {
            "5m": SOURCE_HTF_5M,
            "15m": SOURCE_HTF_15M,
            "1h": SOURCE_HTF_1H,
            "4h": SOURCE_HTF_4H,
            "12h": SOURCE_HTF_12H,
            "1D": SOURCE_HTF_1D,
        }
        if tf not in mapping:
            raise ValueError(f"unknown HTF timeframe: {tf}")
        return mapping[tf]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["htf_pivot_specs"] = {k: dict(v) for k, v in HTF_PIVOT_SPECS.items()}
        d["invalidation_modes"] = [
            INVALIDATION_CLOSE_BREAK_ONLY,
            INVALIDATION_REPLACEMENT_ONLY,
            INVALIDATION_BOTH,
        ]
        d["touch_modes"] = [TOUCH_WICK, TOUCH_CLOSE_DISTANCE, TOUCH_ATR_ZONE]
        return d

    def config_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True, default=str).encode()
        ).hexdigest()


def default_config() -> HtfPivotPreviewConfig:
    return HtfPivotPreviewConfig()


def level_id(
    *,
    symbol: str,
    source_type: str,
    timeframe: str,
    side: str,
    confirmation_timestamp: str,
    level_price: float,
) -> str:
    raw = f"{symbol}|{source_type}|{timeframe}|{side}|{confirmation_timestamp}|{level_price:.10g}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]

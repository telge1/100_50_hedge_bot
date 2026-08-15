"""Orderflow Absorption Pattern Audit — frozen simple config."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

AUDIT_NAME = "orderflow_absorption"
AUDIT_VERSION = "orderflow_absorption_v1"
IMPORT_VERSION_DEFAULT = "derivatives_5m_v1"

UNAVAILABLE_SYMBOLS = frozenset({"ENAUSDT", "ARBUSDT", "OPUSDT"})

DEFAULT_LOOKBACKS = (6, 12, 24)
DEFAULT_HORIZONS = (3, 6, 12)
DEFAULT_MOVE_THRESHOLDS = (0.0025, 0.005, 0.01)

# Flow thresholds
F1_ABS = 0.10
F2_ABS = 0.05
F3_PERCENTILE = 90.0
ROLLING_REF_BARS = 288  # causal prior window for vol median / F3

# Price reaction
NORMAL_PROGRESS_ABS = 0.0025  # 0.25%
WEAK_PROGRESS_ABS = 0.0010  # 0.10%

# Close location
CLOSE_WEAK = 0.50
CLOSE_WEAK_STRONG = 0.35
CLOSE_STRONG = 0.50
CLOSE_STRONG_STRONG = 0.65

MIN_SAMPLE = 30
MIN_SAMPLE_STRONG = 100
MAX_COIN_SHARE_STRONG = 0.70
BAR_SECONDS = 300
ATR_PERIOD = 14

PATTERNS_ABSORPTION = ("A1", "A2", "A3", "A4")
PATTERNS_CONTROL = ("C1", "C2", "C3", "C4", "C5")
FLOW_RULES = ("F1", "F2", "F3")


@dataclass(frozen=True)
class AbsorptionConfig:
    lookbacks: tuple[int, ...] = DEFAULT_LOOKBACKS
    horizons: tuple[int, ...] = DEFAULT_HORIZONS
    move_thresholds: tuple[float, ...] = DEFAULT_MOVE_THRESHOLDS
    f1_abs: float = F1_ABS
    f2_abs: float = F2_ABS
    f3_percentile: float = F3_PERCENTILE
    rolling_ref_bars: int = ROLLING_REF_BARS
    normal_progress_abs: float = NORMAL_PROGRESS_ABS
    weak_progress_abs: float = WEAK_PROGRESS_ABS
    min_sample: int = MIN_SAMPLE
    min_sample_strong: int = MIN_SAMPLE_STRONG
    max_coin_share_strong: float = MAX_COIN_SHARE_STRONG
    import_version: str = IMPORT_VERSION_DEFAULT

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def config_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True, default=str).encode()
        ).hexdigest()


def default_config() -> AbsorptionConfig:
    return AbsorptionConfig()


def thr_label(thr: float) -> str:
    pct = thr * 100.0
    return f"{pct:.2f}".replace(".", "_") + "pct"

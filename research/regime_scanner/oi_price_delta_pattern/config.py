"""OI + Price + Orderflow Delta Pattern Audit — frozen simple config."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

AUDIT_NAME = "oi_price_delta_pattern"
AUDIT_VERSION = "oi_price_delta_pattern_v1"
IMPORT_VERSION_DEFAULT = "derivatives_5m_v1"

AVAILABLE_SYMBOLS = ("BTCUSDT", "ETHUSDT", "APTUSDT")
UNAVAILABLE_SYMBOLS = frozenset({"ENAUSDT", "ARBUSDT", "OPUSDT"})

DEFAULT_LOOKBACKS = (12, 24)
DEFAULT_HORIZONS = (3, 6, 12)
DEFAULT_MOVE_THRESHOLDS = (0.005, 0.01)  # fractions → 0.50%, 1.00%

PRICE_FLAT_ABS = 0.0025  # ±0.25%
DELTA_NEUTRAL_ABS = 0.05  # |delta_ratio| < 0.05 → neutral
MIN_SAMPLE = 30
BAR_SECONDS = 300
ATR_PERIOD = 14

PATTERNS = ("P1", "P2", "P3", "P4", "P5", "P6")

KNOWN_OUTAGE = ("2026-03-25T18:13:00+00:00", "2026-03-27T16:46:00+00:00")


@dataclass(frozen=True)
class PatternConfig:
    lookbacks: tuple[int, ...] = DEFAULT_LOOKBACKS
    horizons: tuple[int, ...] = DEFAULT_HORIZONS
    move_thresholds: tuple[float, ...] = DEFAULT_MOVE_THRESHOLDS
    price_flat_abs: float = PRICE_FLAT_ABS
    delta_neutral_abs: float = DELTA_NEUTRAL_ABS
    min_sample: int = MIN_SAMPLE
    import_version: str = IMPORT_VERSION_DEFAULT

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def config_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True, default=str).encode()
        ).hexdigest()


def default_config() -> PatternConfig:
    return PatternConfig()


def thr_label(thr: float) -> str:
    """0.005 → '0_50pct'."""
    pct = thr * 100.0
    return f"{pct:.2f}".replace(".", "_") + "pct"

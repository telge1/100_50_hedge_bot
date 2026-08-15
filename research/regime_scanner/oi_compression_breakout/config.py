"""OI Compression Breakout Event Audit — frozen research config."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

AUDIT_NAME = "oi_compression_breakout"
AUDIT_VERSION = "oi_compression_breakout_v1"
IMPORT_VERSION_DEFAULT = "derivatives_5m_v1"

AVAILABLE_SYMBOLS = (
    "APTUSDT",
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
UNAVAILABLE_SYMBOLS = frozenset({"ENAUSDT", "ARBUSDT", "OPUSDT"})

# Box lengths (bars @ 5m)
BOX_LENGTHS = (16, 32, 64)
# Quality: max box_width / ATR14
QUALITY_RULES = {"Q1": 2.0, "Q2": 1.5}
BOX_DRIFT_MAX = 0.35
BOX_DRIFT_TIGHT = 0.20  # O4
INNER_CLOSE_MIN_RATIO = 0.75
INNER_ZONE_MARGIN = 0.20  # keep outer 20% each side
ATR_PERIOD = 14
ATR_PCT_LOOKBACK = 288
OI_STEP_RATIO_MIN = 0.65  # O3
OI_PCTL_MIN_HISTORY = 20  # prior boxes before O2 can fire

WAIT_WINDOWS = (3, 6, 12, 24, 48)
MAX_WAIT_BARS = 48

MFE_HORIZONS = (1, 2, 3, 6, 12, 24, 48, 96)
FIRST_TOUCH_PCT = (0.25, 0.50, 0.75, 1.00, 1.50, 2.00)
FIRST_TOUCH_ATR = (0.5, 1.0, 1.5, 2.0)
FIRST_TOUCH_BOX = (0.5, 1.0, 1.5)

EXIT_MODELS: dict[str, tuple[float, float, int]] = {
    "X1": (0.50, 0.50, 12),
    "X2": (0.75, 0.50, 24),
    "X3": (1.00, 0.75, 24),
    "X4": (1.00, 1.00, 48),
    "X5": (1.50, 1.00, 48),
}
EXIT_HOLDS = (12, 24, 48)
COST_PCT = (0.20, 0.25, 0.30)

SPLIT_DEV = ("2026-03-15T00:00:00+00:00", "2026-04-05T00:00:00+00:00")
SPLIT_VAL = ("2026-04-05T00:00:00+00:00", "2026-04-20T00:00:00+00:00")
SPLIT_OOS = ("2026-04-20T00:00:00+00:00", "2026-05-06T00:00:00+00:00")
KNOWN_OUTAGE = ("2026-03-25T18:13:00+00:00", "2026-03-27T16:46:00+00:00")

OI_GROUPS = ("O0", "O1", "O2", "O3", "O4")
BAR_SECONDS = 300


@dataclass(frozen=True)
class OICBConfig:
    audit_name: str = AUDIT_NAME
    audit_version: str = AUDIT_VERSION
    import_version: str = IMPORT_VERSION_DEFAULT
    max_wait_bars: int = MAX_WAIT_BARS
    oi_pctl_min_history: int = OI_PCTL_MIN_HISTORY
    compute_exits: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def config_hash(self) -> str:
        payload = {
            **self.to_dict(),
            "box_lengths": BOX_LENGTHS,
            "quality_rules": QUALITY_RULES,
            "box_drift_max": BOX_DRIFT_MAX,
            "inner_close_min_ratio": INNER_CLOSE_MIN_RATIO,
            "wait_windows": WAIT_WINDOWS,
            "oi_groups": OI_GROUPS,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def default_config() -> OICBConfig:
    return OICBConfig()


def box_variant_id(box_length: int, quality: str) -> str:
    return f"B{box_length}x{quality}"


def candidate_id(*, physical_id: str, box_length: int, quality: str, oi_group: str) -> str:
    return f"{physical_id}|B{box_length}|{quality}|{oi_group}"

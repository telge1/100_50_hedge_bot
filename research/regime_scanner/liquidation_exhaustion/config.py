"""H1 Liquidation Exhaustion Reversal — frozen research config (no free optimization)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

AUDIT_NAME = "liquidation_exhaustion_reversal"
AUDIT_VERSION = "liquidation_exhaustion_reversal_v1"
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

BURST_VARIANTS = ("B1", "B2", "B3", "B4")
PRICE_VARIANTS = ("P1", "P2", "P3")
OI_VARIANTS = ("O0", "O1", "O2", "O3")
RECLAIM_VARIANTS = ("R1", "R2", "R3")
RECLAIM_WINDOWS = (1, 2, 3, 6)

BURST_LOOKBACK = 288  # valid 5m buckets
B2_MAD_MULT = 5.0
COOLDOWN_BARS = 6

MFE_HORIZONS = (1, 2, 3, 6, 12, 24, 48, 96)
FIRST_TOUCH_PCT = (0.25, 0.50, 0.75, 1.00, 1.50, 2.00)
FIRST_TOUCH_ATR = (0.5, 1.0, 1.5, 2.0)

# exit_id -> (tp_pct, sl_pct, max_hold) — costs applied separately
EXIT_MODELS: dict[str, tuple[float, float, int]] = {
    "X1": (0.50, 0.50, 12),
    "X2": (0.75, 0.50, 24),
    "X3": (1.00, 0.75, 24),
    "X4": (1.00, 1.00, 48),
    "X5": (1.50, 1.00, 48),
}
# Override holds per model as specified: 12, 24, 48 used across matrix in audit runner
EXIT_HOLDS = (12, 24, 48)
COST_PCT = (0.20, 0.25, 0.30)

# Chrono splits (UTC, end exclusive for next)
SPLIT_DEV = ("2026-03-15T00:00:00+00:00", "2026-04-05T00:00:00+00:00")
SPLIT_VAL = ("2026-04-05T00:00:00+00:00", "2026-04-20T00:00:00+00:00")
SPLIT_OOS = ("2026-04-20T00:00:00+00:00", "2026-05-06T00:00:00+00:00")

KNOWN_OUTAGE = ("2026-03-25T18:13:00+00:00", "2026-03-27T16:46:00+00:00")


@dataclass(frozen=True)
class LEConfig:
    audit_name: str = AUDIT_NAME
    audit_version: str = AUDIT_VERSION
    import_version: str = IMPORT_VERSION_DEFAULT
    burst_lookback: int = BURST_LOOKBACK
    b2_mad_mult: float = B2_MAD_MULT
    cooldown_bars: int = COOLDOWN_BARS

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def config_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True, default=str).encode()
        ).hexdigest()


def default_config() -> LEConfig:
    return LEConfig()


def variant_id(burst: str, price: str, oi: str, reclaim: str, window: int) -> str:
    return f"{burst}x{price}x{oi}x{reclaim}w{window}"

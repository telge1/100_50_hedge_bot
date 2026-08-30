"""Liquidity Pool Signal — detection foundation contracts only.

No trading signal, entry, exit, walls, or nested strategy.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Optional


class PoolSide(str, Enum):
    ASK = "ASK"
    BID = "BID"


class MarketPoolLocation(str, Enum):
    BETWEEN_POOLS = "BETWEEN_POOLS"
    INSIDE_ASK_POOL = "INSIDE_ASK_POOL"
    INSIDE_BID_POOL = "INSIDE_BID_POOL"
    INSIDE_OVERLAPPING_POOLS = "INSIDE_OVERLAPPING_POOLS"
    NO_ACTIVE_POOLS = "NO_ACTIVE_POOLS"


@dataclass(frozen=True)
class PoolSnapshot:
    """Normalized chart Liquidity Location pool at an as-of time.

    Fields map 1:1 to the chart engine / availability contract.
    """

    pool_id: str
    symbol: str
    source_timeframe: str
    side: PoolSide
    lower_edge: float
    upper_edge: float
    strength: Optional[float]
    origin_ts: Optional[str]  # source_timestamp (swing bar open)
    available_at: str
    invalidated_at: Optional[str]
    active_as_of: bool

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.value
        return d

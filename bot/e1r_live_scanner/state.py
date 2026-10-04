"""Per-symbol isolated scanner state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Readiness(str, Enum):
    WARMING = "WARMING"
    LIVE_READY = "LIVE_READY"
    WAITING_FOR_DATA = "WAITING_FOR_DATA"
    DATA_MISSING = "DATA_MISSING"


@dataclass
class SymbolState:
    symbol: str
    readiness: Readiness = Readiness.WARMING
    last_processed_15m_close: datetime | None = None
    data_missing_reason: str | None = None

    candles15: Any = None
    bars15: list[dict] = field(default_factory=list)
    by_open: dict = field(default_factory=dict)
    market: dict | None = None
    lld_cache: dict = field(default_factory=dict)
    lld_cfg: Any = None

    floor_guard: Any = None
    seen_no_guard: set[str] = field(default_factory=set)
    seen_with_guard: set[str] = field(default_factory=set)

    e1r_engine: Any = None
    state_ts: dict[str, str] = field(default_factory=dict)

    def note_ts(self, key: str, moment: datetime) -> None:
        self.state_ts[key] = moment.isoformat()

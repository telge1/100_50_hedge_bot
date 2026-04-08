from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Emergency100Mode(Enum):
    IDLE = "idle"
    FREEZE = "freeze"
    PING_PONG = "ping_pong"
    BRIDGE_TO_NORMAL = "bridge_to_normal"
    READY_FOR_HANDOFF = "ready_for_handoff"


class MarketBias(Enum):
    FALLING = "falling"
    RISING = "rising"
    UNCLEAR = "unclear"


@dataclass
class HedgeSnapshot:
    symbol: str
    current_price: float
    long_size_usdt: float
    short_size_usdt: float
    long_avg: float
    short_avg: float
    atr_pct: float | None = None
    price_speed_pct: float | None = None

    @property
    def spread_pct(self) -> float:
        if self.long_avg <= 0 or self.short_avg <= 0:
            return 0.0
        return max((self.long_avg - self.short_avg) / self.long_avg, 0.0)

    @property
    def short_ratio(self) -> float:
        if self.long_size_usdt <= 0:
            return 0.0
        return self.short_size_usdt / self.long_size_usdt


@dataclass
class Emergency100RuntimeState:
    mode: Emergency100Mode = Emergency100Mode.IDLE
    bridge_step_index: int = 0
    cycle_id: str | None = None
    decision_count: int = 0
    last_decision_id: str = ""
    last_action: str = "none"
    last_reason: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "bridge_step_index": self.bridge_step_index,
            "cycle_id": self.cycle_id,
            "decision_count": self.decision_count,
            "last_decision_id": self.last_decision_id,
            "last_action": self.last_action,
            "last_reason": self.last_reason,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object] | None) -> "Emergency100RuntimeState":
        if not data:
            return cls()
        mode_value = str(data.get("mode") or Emergency100Mode.IDLE.value)
        try:
            mode = Emergency100Mode(mode_value)
        except ValueError:
            mode = Emergency100Mode.IDLE
        return cls(
            mode=mode,
            bridge_step_index=int(data.get("bridge_step_index") or 0),
            cycle_id=str(data.get("cycle_id") or "") or None,
            decision_count=int(data.get("decision_count") or 0),
            last_decision_id=str(data.get("last_decision_id") or ""),
            last_action=str(data.get("last_action") or "none"),
            last_reason=str(data.get("last_reason") or ""),
            notes=[str(item) for item in (data.get("notes") or [])],
        )

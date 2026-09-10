"""Causal cooldown bookkeeping (no future peak search)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any


@dataclass
class CooldownBook:
    cooldown_seconds: int
    last_trigger: dict[str, datetime] = field(default_factory=dict)

    def allow(self, candidate_type: str, trigger_ts: datetime) -> bool:
        last = self.last_trigger.get(candidate_type)
        if last is None:
            return True
        return trigger_ts >= last + timedelta(seconds=self.cooldown_seconds)

    def register(self, candidate_type: str, trigger_ts: datetime) -> None:
        self.last_trigger[candidate_type] = trigger_ts

    def snapshot(self) -> dict[str, Any]:
        return {
            "cooldown_seconds": self.cooldown_seconds,
            "last_trigger": {k: v.isoformat().replace("+00:00", "Z") for k, v in self.last_trigger.items()},
        }

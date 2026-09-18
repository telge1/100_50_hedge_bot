"""Injectable clocks for real / offline observation timing."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol
import time


class ObservationClock(Protocol):
    def now(self) -> datetime: ...

    def sleep(self, seconds: float) -> None: ...


@dataclass
class RealClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


@dataclass
class FakeClock:
    """Deterministic clock; sleep advances time without wall wait."""

    _now: datetime
    sleeps: list[float] = field(default_factory=list)

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        if seconds > 0:
            self._now = self._now + timedelta(seconds=float(seconds))

    def advance(self, seconds: float) -> None:
        self.sleep(seconds)

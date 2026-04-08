from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Order:
    side: str
    size: float
    price: float
    filled_size: float
    timestamp: datetime

    @property
    def remaining(self) -> float:
        return max(0.0, self.size - self.filled_size)

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Position:
    side: str
    size: float = 0.0
    avg_price: float = 0.0

    def update(self, fill_size: float, fill_price: float) -> None:
        if fill_size <= 0:
            return

        total_cost = self.avg_price * self.size + fill_price * fill_size
        self.size += fill_size
        self.avg_price = total_cost / self.size if self.size else 0.0

    def reduce(self, close_size: float) -> float:
        close_size = min(close_size, self.size)
        self.size -= close_size
        if self.size == 0:
            self.avg_price = 0.0
        return close_size

    def notional(self) -> float:
        return self.size * self.avg_price

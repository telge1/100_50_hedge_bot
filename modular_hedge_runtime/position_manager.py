from __future__ import annotations

from datetime import datetime
from typing import Tuple

from models.position import Position


class PositionManager:
    def __init__(self) -> None:
        self.long = Position(side="long")
        self.short = Position(side="short")
        self.last_long_fill: datetime | None = None
        self.last_short_fill: datetime | None = None

    def apply_fill(
        self, side: str, size: float, price: float, max_fill: float
    ) -> Tuple[float, float]:
        fill_size = min(size, max_fill)
        if fill_size <= 0:
            return 0.0, price

        if side == "long":
            self.long.update(fill_size, price)
            self.last_long_fill = datetime.utcnow()
        else:
            self.short.update(fill_size, price)
            self.last_short_fill = datetime.utcnow()

        return fill_size, price

    def reduce(self, side: str, size: float) -> float:
        if side == "long":
            return self.long.reduce(size)
        return self.short.reduce(size)

    def total_notional(self) -> float:
        return self.long.notional() + self.short.notional()

    @property
    def long_size(self) -> float:
        return self.long.size

    @property
    def short_size(self) -> float:
        return self.short.size

    @property
    def long_avg(self) -> float:
        return self.long.avg_price

    @property
    def short_avg(self) -> float:
        return self.short.avg_price

    def average_prices(self) -> tuple[float, float]:
        return self.long_avg, self.short_avg

    def sync_positions(
        self,
        long_size: float,
        long_avg: float,
        short_size: float,
        short_avg: float,
    ) -> None:
        self.long.size = long_size
        self.long.avg_price = long_avg if long_size else 0.0
        self.short.size = short_size
        self.short.avg_price = short_avg if short_size else 0.0

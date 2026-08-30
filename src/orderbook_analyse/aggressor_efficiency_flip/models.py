"""Typed models for AEF F0."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Optional


@dataclass
class Trade:
    trade_ts: datetime
    trade_id: str
    side: str  # Buy | Sell
    price: float
    size: float
    notional: float


@dataclass
class SecondBucket:
    sec: datetime  # floor second UTC
    buy_count: int = 0
    sell_count: int = 0
    buy_qty: float = 0.0
    sell_qty: float = 0.0
    buy_notional: float = 0.0
    sell_notional: float = 0.0
    first_price: Optional[float] = None
    last_price: Optional[float] = None
    high_price: Optional[float] = None
    low_price: Optional[float] = None
    trade_count: int = 0
    # Note: first/last use (trade_ts, trade_id) order for stability only;
    # trade_id does NOT claim exchange micro-order within identical ms.

    @property
    def total_notional(self) -> float:
        return self.buy_notional + self.sell_notional

    @property
    def net_aggressive_notional(self) -> float:
        return self.buy_notional - self.sell_notional

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["sec"] = self.sec.isoformat().replace("+00:00", "Z")
        return d


@dataclass
class WindowMetrics:
    start: datetime
    end: datetime
    buy_count: int
    sell_count: int
    buy_qty: float
    sell_qty: float
    buy_notional: float
    sell_notional: float
    first_price: Optional[float]
    last_price: Optional[float]
    high_price: Optional[float]
    low_price: Optional[float]
    seconds_with_trades: int
    span_seconds: int
    empty: bool = False

    @property
    def total_notional(self) -> float:
        return self.buy_notional + self.sell_notional

    @property
    def coverage(self) -> float:
        return self.seconds_with_trades / self.span_seconds if self.span_seconds else 0.0

    def dominant_side(self) -> str:
        if self.buy_notional > self.sell_notional:
            return "Buy"
        if self.sell_notional > self.buy_notional:
            return "Sell"
        return "FLAT"

    def dominant_share(self) -> float:
        tot = self.total_notional
        if tot <= 0:
            return 0.0
        return max(self.buy_notional, self.sell_notional) / tot


@dataclass
class StateTransition:
    episode_id: str
    symbol: str
    direction: str
    event_ts: datetime
    decision_ts: datetime
    from_state: str
    to_state: str
    reason_code: str
    closed_windows: str
    data_quality: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("event_ts", "decision_ts"):
            d[k] = getattr(self, k).isoformat().replace("+00:00", "Z")
        return d


@dataclass
class Episode:
    fields: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.fields)

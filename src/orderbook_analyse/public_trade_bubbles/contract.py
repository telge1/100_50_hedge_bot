"""Canonical public-trade + bubble contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass(frozen=True)
class PublicTradeRecord:
    trade_id: str
    symbol: str
    trade_timestamp: datetime
    price: float
    quantity_base: float
    notional_quote: float
    taker_side: str  # Buy | Sell
    is_aggressive_buy: bool
    is_aggressive_sell: bool
    source: str
    source_quality: str
    received_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["trade_timestamp"] = _iso(self.trade_timestamp)
        d["received_at"] = _iso(self.received_at)
        return d


@dataclass
class BubbleRecord:
    bubble_id: str
    symbol: str
    bucket_start: datetime
    bucket_end: datetime
    price: float
    buy_notional: float
    sell_notional: float
    total_notional: float
    delta_notional: float
    trade_count: int
    max_single_trade_notional: float
    dominant_side: str
    size_class: str
    known_at: datetime
    forming: bool = False
    source_quality: str = "ok"
    normalization_window_start: datetime | None = None
    normalization_window_end: datetime | None = None
    sample_count: int = 0
    threshold_medium: float | None = None
    threshold_large: float | None = None
    threshold_extreme: float | None = None
    max_feature_timestamp: datetime | None = None
    # optional research context (filled later)
    bubble_inside_pool: bool | None = None
    distance_to_pool_ticks: float | None = None
    pool_id: str | None = None
    pool_side: str | None = None
    pool_status_at_bubble: str | None = None
    pool_available_at: datetime | None = None
    distance_to_ema200_atr: float | None = None
    buy_notional_inside_pool: float | None = None
    sell_notional_inside_pool: float | None = None
    delta_inside_pool: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in (
            "bucket_start",
            "bucket_end",
            "known_at",
            "normalization_window_start",
            "normalization_window_end",
            "max_feature_timestamp",
            "pool_available_at",
        ):
            d[k] = _iso(d.get(k))
        return d


def aggressor_flags(taker_side: str) -> tuple[bool, bool]:
    side = str(taker_side or "").strip()
    # Accept Buy/Sell and B/S
    if side in ("Buy", "BUY", "B", "buy"):
        return True, False
    if side in ("Sell", "SELL", "S", "sell"):
        return False, True
    return False, False

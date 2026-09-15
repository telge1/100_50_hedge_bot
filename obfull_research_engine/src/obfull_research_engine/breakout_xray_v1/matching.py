"""Trade ↔ wall matching with explicit confidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .ports import LevelChangeEvent, MidState
from .trades import XRayTrade
from .time_windows import dt_to_ns


@dataclass(frozen=True)
class WallMatchResult:
    side: str
    price: float
    first_touch_ns: int | None
    first_aggressive_touch_ns: int | None
    last_aggressive_touch_ns: int | None
    hit_trade_count: int
    hit_notional_usdt: float
    size_before_touch: float | None
    minimum_size_after_touch: float | None
    refill_size: float | None
    price_crossed: bool
    price_reclaimed: bool
    matching_confidence: str  # HIGH | MEDIUM | LOW

    def to_dict(self) -> dict:
        return {
            "side": self.side,
            "price": self.price,
            "first_touch_ns": self.first_touch_ns,
            "first_aggressive_touch_ns": self.first_aggressive_touch_ns,
            "last_aggressive_touch_ns": self.last_aggressive_touch_ns,
            "hit_trade_count": self.hit_trade_count,
            "hit_notional_usdt": self.hit_notional_usdt,
            "size_before_touch": self.size_before_touch,
            "minimum_size_after_touch": self.minimum_size_after_touch,
            "refill_size": self.refill_size,
            "price_crossed": self.price_crossed,
            "price_reclaimed": self.price_reclaimed,
            "matching_confidence": self.matching_confidence,
        }


def _trade_hits_level(
    *,
    trade: XRayTrade,
    side: str,
    price: float,
    tick_tolerance: float,
) -> bool:
    """Ask-wall hit by Buy; Bid-wall hit by Sell within tick tolerance."""
    if side == "ask" and trade.side != "Buy":
        return False
    if side == "bid" and trade.side != "Sell":
        return False
    return abs(float(trade.price) - float(price)) <= float(tick_tolerance)


def match_trades_to_wall(
    *,
    side: str,
    price: float,
    baseline_size: float,
    events: Sequence[LevelChangeEvent],
    trades: Sequence[XRayTrade],
    mid_series: Sequence[MidState],
    tick_tolerance: float = 0.05,
) -> WallMatchResult:
    hits = [
        t
        for t in trades
        if _trade_hits_level(
            trade=t, side=side, price=price, tick_tolerance=tick_tolerance
        )
    ]
    hit_ns = [dt_to_ns(t.trade_ts) for t in hits]
    first_agg = min(hit_ns) if hit_ns else None
    last_agg = max(hit_ns) if hit_ns else None
    hit_notional = sum(float(t.notional) for t in hits)

    # Mid cross / reclaim relative to level
    price_crossed = False
    price_reclaimed = False
    if mid_series:
        mids = [m.mid for m in mid_series]
        if side == "ask":
            price_crossed = any(m > price + tick_tolerance for m in mids)
            if price_crossed:
                # reclaim = later mid back below
                crossed_i = next(
                    i for i, m in enumerate(mids) if m > price + tick_tolerance
                )
                price_reclaimed = any(
                    m < price - tick_tolerance for m in mids[crossed_i:]
                )
        else:
            price_crossed = any(m < price - tick_tolerance for m in mids)
            if price_crossed:
                crossed_i = next(
                    i for i, m in enumerate(mids) if m < price - tick_tolerance
                )
                price_reclaimed = any(
                    m > price + tick_tolerance for m in mids[crossed_i:]
                )

    size_before = baseline_size
    min_after = None
    refill = None
    first_touch_ns = None
    if events:
        first_touch_ns = min(e.event_time_ns for e in events)
        if first_agg is not None:
            after = [e for e in events if e.event_time_ns >= first_agg]
            if after:
                sizes = [e.new_size for e in after]
                min_after = min(sizes)
                # refill = recovery after min
                min_i = sizes.index(min_after)
                if min_i + 1 < len(sizes):
                    refill = max(sizes[min_i + 1 :])

    # Confidence
    if not hits and not events:
        conf = "LOW"
    elif hits and first_agg is not None and events:
        # require some event near first hit (±2s)
        near = any(abs(e.event_time_ns - first_agg) <= 2_000_000_000 for e in events)
        conf = "HIGH" if near else "MEDIUM"
    elif hits or events:
        conf = "MEDIUM"
    else:
        conf = "LOW"

    return WallMatchResult(
        side=side,
        price=price,
        first_touch_ns=first_touch_ns,
        first_aggressive_touch_ns=first_agg,
        last_aggressive_touch_ns=last_agg,
        hit_trade_count=len(hits),
        hit_notional_usdt=hit_notional,
        size_before_touch=size_before,
        minimum_size_after_touch=min_after,
        refill_size=refill,
        price_crossed=price_crossed,
        price_reclaimed=price_reclaimed,
        matching_confidence=conf,
    )

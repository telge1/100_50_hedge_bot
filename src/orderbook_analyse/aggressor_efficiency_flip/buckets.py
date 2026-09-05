"""1s buckets and window aggregation from trades."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, Optional

from orderbook_analyse.aggressor_efficiency_flip.models import SecondBucket, Trade, WindowMetrics
from orderbook_analyse.aggressor_efficiency_flip.timeutil import ensure_utc, floor_second


def sort_trades(trades: Iterable[Trade]) -> list[Trade]:
    """Stable processing order: (trade_ts, trade_id).

    trade_id is a deterministic tie-break only — it does NOT assert exchange
    micro-sequence within identical milliseconds.
    """
    return sorted(
        trades,
        key=lambda t: (ensure_utc(t.trade_ts), str(t.trade_id)),
    )


def build_second_buckets(trades: Iterable[Trade]) -> dict[datetime, SecondBucket]:
    """Assign each trade to exactly one 1s bucket via floor(trade_ts)."""
    buckets: dict[datetime, SecondBucket] = {}
    for tr in sort_trades(trades):
        if tr.side not in {"Buy", "Sell"}:
            raise ValueError(f"invalid aggressor side {tr.side!r}")
        if tr.price <= 0 or tr.size <= 0 or tr.notional < 0:
            continue
        sec = floor_second(tr.trade_ts)
        b = buckets.get(sec)
        if b is None:
            b = SecondBucket(sec=sec)
            buckets[sec] = b
        b.trade_count += 1
        if tr.side == "Buy":
            b.buy_count += 1
            b.buy_qty += tr.size
            b.buy_notional += tr.notional
        else:
            b.sell_count += 1
            b.sell_qty += tr.size
            b.sell_notional += tr.notional
        px = float(tr.price)
        # first/last by sorted order already — first write wins for first_price
        if b.first_price is None:
            b.first_price = px
        b.last_price = px
        b.high_price = px if b.high_price is None else max(b.high_price, px)
        b.low_price = px if b.low_price is None else min(b.low_price, px)
    return buckets


def aggregate_window(
    buckets: dict[datetime, SecondBucket],
    start: datetime,
    end: datetime,
) -> WindowMetrics:
    """Aggregate half-open [start, end) from 1s buckets."""
    start = floor_second(start)
    end = floor_second(end)
    if not (end > start):
        raise ValueError("window end must be > start")
    span = int((end - start).total_seconds())
    buy_c = sell_c = 0
    buy_q = sell_q = buy_n = sell_n = 0.0
    first: Optional[float] = None
    last: Optional[float] = None
    hi: Optional[float] = None
    lo: Optional[float] = None
    secs = 0
    cur = start
    while cur < end:
        b = buckets.get(cur)
        if b is not None and b.trade_count > 0:
            secs += 1
            buy_c += b.buy_count
            sell_c += b.sell_count
            buy_q += b.buy_qty
            sell_q += b.sell_qty
            buy_n += b.buy_notional
            sell_n += b.sell_notional
            if b.first_price is not None:
                if first is None:
                    first = b.first_price
                last = b.last_price
                hi = b.high_price if hi is None else max(hi, b.high_price or hi)
                lo = b.low_price if lo is None else min(lo, b.low_price or lo)
        cur += timedelta(seconds=1)
    empty = secs == 0 or first is None or last is None
    return WindowMetrics(
        start=start,
        end=end,
        buy_count=buy_c,
        sell_count=sell_c,
        buy_qty=buy_q,
        sell_qty=sell_q,
        buy_notional=buy_n,
        sell_notional=sell_n,
        first_price=first,
        last_price=last,
        high_price=hi,
        low_price=lo,
        seconds_with_trades=secs,
        span_seconds=span,
        empty=empty,
    )


def side_vwap(trades: Iterable[Trade], start: datetime, end: datetime, side: str) -> Optional[float]:
    """Notional-weighted VWAP for side in [start, end)."""
    start = ensure_utc(start)
    end = ensure_utc(end)
    num = den = 0.0
    for tr in sort_trades(trades):
        ts = ensure_utc(tr.trade_ts)
        if ts < start or ts >= end:
            continue
        if tr.side != side:
            continue
        num += tr.price * tr.notional
        den += tr.notional
    if den <= 0:
        return None
    return num / den

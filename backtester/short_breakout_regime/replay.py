"""Replay the short blocker from a start time up to now.

Pools are filtered by their birth and invalidation times. A short block is
reported as a stretch of hourly decision times, not as a trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from short_breakout_regime.short_block import (
    PoolMark,
    ShortBreakoutContext,
    should_block_short,
)


@dataclass(frozen=True)
class OhlcBar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class TimedPool:
    bottom: float
    top: float
    strength: float
    born_ts: datetime
    invalidated_ts: datetime | None = None


@dataclass(frozen=True)
class BlockSample:
    ts: datetime
    block_short: bool
    reason: str
    price: float
    breakout_ts: datetime | None = None


@dataclass(frozen=True)
class BlockRange:
    start: datetime
    end: datetime


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def floor_hours(ts: datetime, hours: int) -> datetime:
    ts = _utc(ts)
    hour = (ts.hour // hours) * hours
    return ts.replace(hour=hour, minute=0, second=0, microsecond=0)


def aggregate_ohlc(bars: list[OhlcBar], *, hours: int) -> list[OhlcBar]:
    """Complete buckets only. 1h needs 12 closed 5m bars, 4h needs 48."""
    need = hours * 12
    buckets: dict[datetime, list[OhlcBar]] = {}
    for bar in bars:
        buckets.setdefault(floor_hours(bar.ts, hours), []).append(bar)
    out: list[OhlcBar] = []
    for key in sorted(buckets):
        group = sorted(buckets[key], key=lambda b: _utc(b.ts))
        if len(group) < need:
            continue
        out.append(
            OhlcBar(
                ts=key,
                open=float(group[0].open),
                high=max(float(b.high) for b in group),
                low=min(float(b.low) for b in group),
                close=float(group[-1].close),
            )
        )
    return out


def ema_series(closes: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) < period:
        return out
    k = 2.0 / (period + 1.0)
    value = sum(closes[:period]) / period
    out[period - 1] = value
    for i, px in enumerate(closes[period:], start=period):
        value = px * k + value * (1.0 - k)
        out[i] = float(value)
    return out


def active_pools(pools: list[TimedPool], ts: datetime) -> list[TimedPool]:
    ts = _utc(ts)
    return [
        pool
        for pool in pools
        if _utc(pool.born_ts) <= ts and (pool.invalidated_ts is None or _utc(pool.invalidated_ts) > ts)
    ]


def _stack(ema9: float | None, ema20: float | None, ema59: float | None) -> str:
    if ema9 is None or ema20 is None or ema59 is None:
        return "unknown"
    if ema9 > ema59 and ema20 > ema59:
        return "bullish"
    if ema9 < ema59 and ema20 < ema59:
        return "bearish"
    return "neutral"


def _combine(h1: str, h4: str) -> str:
    if h1 == "unknown" or h4 == "unknown":
        return "unknown"
    if h1 == "bullish" or h4 == "bullish":
        return "bullish"
    if h1 == "bearish" and h4 == "bearish":
        return "bearish"
    return "neutral"


def _band_cleared(close: float, ema9: float, ema20: float, ema59: float, ema200: float) -> bool:
    """Close is above every band, not just a nick of EMA200.

    EMA9, EMA20 and EMA59 lag the candle, so they do not have to sit above
    EMA200 yet. While EMA9 is still under EMA200, the close has to be at least
    that gap above EMA200. That drops a poke that only ticks the slow line.
    """
    if not (close > ema9 and close > ema20 and close > ema59 and close > ema200):
        return False
    gap = ema200 - ema9
    if gap <= 0:
        return True
    return (close - ema200) >= gap


def _active_cross(
    bars: list[OhlcBar],
    last: int,
    ema9: list[float | None],
    ema20: list[float | None],
    ema59: list[float | None],
    ema200: list[float | None],
) -> int | None:
    """Latest bar where price closed up through the EMA band from below.

    The cross stays while closes hold EMA200. A dip through EMA9 or EMA20
    does not end it. A new cross needs a close back under EMA200 first.
    """
    cross: int | None = None
    armed = False
    for i in range(last + 1):
        e9, e20, e59, e200 = ema9[i], ema20[i], ema59[i], ema200[i]
        if e9 is None or e20 is None or e59 is None or e200 is None:
            continue
        close = bars[i].close
        if cross is None:
            if close < e200:
                armed = True
            if armed and _band_cleared(close, e9, e20, e59, e200):
                cross = i
                armed = False
        elif close < e200:
            cross = None
            armed = True
    return cross


def context_at(
    *,
    at: datetime,
    h1: list[OhlcBar],
    h1_ema9: list[float | None],
    h1_label: str,
    h4_label: str,
    pools: list[TimedPool],
    bar_hours: int = 1,
    h1_ema20: list[float | None] | None = None,
    h1_ema59: list[float | None] | None = None,
    h1_ema200: list[float | None] | None = None,
) -> ShortBreakoutContext | None:
    at = _utc(at)
    closed_idx = [i for i, bar in enumerate(h1) if _utc(bar.ts) + timedelta(hours=bar_hours) <= at]
    if not closed_idx:
        return None
    last = closed_idx[-1]
    bar = h1[last]
    regime = _combine(h1_label, h4_label)
    live = active_pools(pools, at)
    marks = tuple(
        PoolMark(bottom=p.bottom, top=p.top, strength=p.strength, born_ts=_utc(p.born_ts)) for p in live
    )
    breakout_ts = None
    breakout_base = None
    held9 = False
    held20 = False
    crossed = False
    if h1_ema20 is not None and h1_ema59 is not None and h1_ema200 is not None:
        cross = _active_cross(h1, last, h1_ema9, h1_ema20, h1_ema59, h1_ema200)
        if cross is not None:
            breakout_ts = _utc(h1[cross].ts)
            crossed = True
            held9 = True
            held20 = True
    return ShortBreakoutContext(
        price=float(bar.close),
        regime=regime,
        h1_label=h1_label,
        h4_label=h4_label,
        breakout_ts=breakout_ts,
        breakout_base=breakout_base,
        ema9_held_since_breakout=held9,
        ema20_held_since_breakout=held20,
        crossed_from_below=crossed,
        pools=marks,
    )


def _bucket_hours(bars: list[OhlcBar]) -> int:
    if len(bars) < 2:
        return 1
    delta = _utc(bars[1].ts) - _utc(bars[0].ts)
    hours = int(delta.total_seconds() // 3600)
    return max(1, hours)


def _label_closed(
    bars: list[OhlcBar],
    ema9: list[float | None],
    ema20: list[float | None],
    ema59: list[float | None],
    at: datetime,
) -> str:
    if not bars:
        return "unknown"
    step = _bucket_hours(bars)
    idx = None
    for i, bar in enumerate(bars):
        if _utc(bar.ts) + timedelta(hours=step) <= _utc(at):
            idx = i
    if idx is None:
        return "unknown"
    return _stack(ema9[idx], ema20[idx], ema59[idx])


def walk_hours(
    *,
    start: datetime,
    end: datetime,
    bars_5m: list[OhlcBar],
    pools: list[TimedPool],
) -> list[BlockSample]:
    start = _utc(start)
    end = _utc(end)
    # The launch is the 4h candle. Its open is the breakout, and the short
    # block starts when that candle has closed.
    h4 = aggregate_ohlc(bars_5m, hours=4)
    closes4 = [b.close for b in h4]
    ema9_4 = ema_series(closes4, 9)
    ema20_4 = ema_series(closes4, 20)
    ema59_4 = ema_series(closes4, 59)
    ema200_4 = ema_series(closes4, 200)

    samples: list[BlockSample] = []
    cursor = floor_hours(start, 4)
    while cursor + timedelta(hours=4) <= end:
        decision = cursor + timedelta(hours=4)
        label = _label_closed(h4, ema9_4, ema20_4, ema59_4, decision)
        ctx = context_at(
            at=decision,
            h1=h4,
            h1_ema9=ema9_4,
            h1_label=label,
            h4_label=label,
            pools=pools,
            bar_hours=4,
            h1_ema20=ema20_4,
            h1_ema59=ema59_4,
            h1_ema200=ema200_4,
        )
        if ctx is not None:
            decision_row = should_block_short(ctx)
            samples.append(
                BlockSample(
                    ts=decision,
                    block_short=decision_row.block_short,
                    reason=decision_row.reason,
                    price=ctx.price,
                    breakout_ts=ctx.breakout_ts,
                )
            )
        cursor += timedelta(hours=4)
    return samples


def block_ranges(samples: list[BlockSample]) -> list[BlockRange]:
    ranges: list[BlockRange] = []
    start: datetime | None = None
    end: datetime | None = None
    for sample in samples:
        if sample.block_short:
            if start is None:
                start = sample.ts
            end = sample.ts
            continue
        if start is not None and end is not None:
            ranges.append(BlockRange(start=start, end=end))
            start = None
            end = None
    if start is not None and end is not None:
        ranges.append(BlockRange(start=start, end=end))
    return ranges

"""Causal 5m candles aggregated from 1m candles (UTC fixed buckets)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from obfull_research_engine.mp_price_path_4h_v1.candles import Candle1m

from .params import NS

FIVE_MIN_NS = 5 * 60 * NS
ONE_MIN_NS = 60 * NS


@dataclass(frozen=True)
class Candle5m:
    open_time_ns: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def close_time_ns(self) -> int:
        """Exclusive end / moment close is known."""
        return self.open_time_ns + FIVE_MIN_NS


def floor_5m_ns(ts_ns: int) -> int:
    return (int(ts_ns) // FIVE_MIN_NS) * FIVE_MIN_NS


def first_allowed_5m_open_after_alert(alert_ts_ns: int) -> int:
    """First 5m bucket that begins strictly after alert_ts.

    Example: alert 16:44:17 → bucket 16:40–16:45 contains alert and is forbidden;
    first allowed open = 16:45.
    """
    containing = floor_5m_ns(alert_ts_ns)
    return containing + FIVE_MIN_NS


def aggregate_5m_from_1m(candles_1m: Sequence[Candle1m]) -> list[Candle5m]:
    """Aggregate contiguous 1m bars into fixed UTC 5m buckets. Incomplete buckets dropped."""
    by_bucket: dict[int, list[Candle1m]] = {}
    for c in candles_1m:
        b = floor_5m_ns(c.open_time_ns)
        by_bucket.setdefault(b, []).append(c)
    out: list[Candle5m] = []
    for open_ns in sorted(by_bucket):
        bars = sorted(by_bucket[open_ns], key=lambda x: x.open_time_ns)
        if len(bars) != 5:
            continue
        # require exact minute alignment within bucket
        ok = True
        for i, bar in enumerate(bars):
            if bar.open_time_ns != open_ns + i * ONE_MIN_NS:
                ok = False
                break
        if not ok:
            continue
        out.append(
            Candle5m(
                open_time_ns=open_ns,
                open=bars[0].open,
                high=max(b.high for b in bars),
                low=min(b.low for b in bars),
                close=bars[-1].close,
                volume=sum(b.volume for b in bars),
            )
        )
    return out


def post_alert_5m(candles_5m: Sequence[Candle5m], *, alert_ts_ns: int) -> list[Candle5m]:
    first = first_allowed_5m_open_after_alert(alert_ts_ns)
    return [c for c in candles_5m if c.open_time_ns >= first]

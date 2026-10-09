"""Measurements on 4h bars and pools. The state machine decides the phase."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from pool_state_maschine.coin_profile import CoinProfile
from pool_state_maschine.market_data import Bar, Pool


@dataclass(frozen=True)
class Observation:
    as_of: datetime
    price: float
    high: float
    low: float
    e9: float
    e20: float
    e59: float
    e200: float


def ema(closes: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) < period:
        return out
    k = 2.0 / (period + 1)
    value = sum(closes[:period]) / period
    out[period - 1] = value
    for index, price in enumerate(closes[period:], start=period):
        value = price * k + value * (1.0 - k)
        out[index] = float(value)
    return out


def _gap_pct(lower: float, upper: float) -> float:
    if lower <= 0:
        return 0.0
    return (upper - lower) / lower * 100.0


def cluster_count(pools: list[Pool], *, gap_pct: float) -> int:
    """How many pools sit in the widest overlapping group."""
    if not pools:
        return 0
    ordered = sorted(pools, key=lambda pool: (pool.bottom, pool.top))
    best = 1
    current = 1
    edge = ordered[0].top
    for pool in ordered[1:]:
        if pool.bottom <= edge or _gap_pct(edge, pool.bottom) < gap_pct:
            current += 1
            edge = max(edge, pool.top)
        else:
            best = max(best, current)
            current = 1
            edge = pool.top
    return max(best, current)


def observations(bars: list[Bar]) -> list[Observation]:
    closes = [bar.close for bar in bars]
    e9 = ema(closes, 9)
    e20 = ema(closes, 20)
    e59 = ema(closes, 59)
    e200 = ema(closes, 200)
    out: list[Observation] = []
    for index, bar in enumerate(bars):
        if e9[index] is None or e20[index] is None or e59[index] is None or e200[index] is None:
            continue
        out.append(
            Observation(
                as_of=bar.ts + timedelta(hours=4),
                price=bar.close,
                high=bar.high,
                low=bar.low,
                e9=float(e9[index]),
                e20=float(e20[index]),
                e59=float(e59[index]),
                e200=float(e200[index]),
            )
        )
    return out


def band_cleared(obs: Observation, *, armed: bool) -> bool:
    """Close has come from below EMA200 and cleared the fast bands by a real margin."""
    if not armed:
        return False
    if not (obs.price > obs.e9 and obs.price > obs.e20 and obs.price > obs.e59 and obs.price > obs.e200):
        return False
    if obs.e9 < obs.e200:
        return (obs.price - obs.e200) >= (obs.e200 - obs.e9)
    return True


def above_fast(obs: Observation) -> bool:
    return obs.price > obs.e9 and obs.price > obs.e20


def lost_fast(obs: Observation) -> bool:
    return obs.price < obs.e9 and obs.price < obs.e20


def bands_rolling_down(obs: Observation) -> bool:
    return obs.e9 < obs.e20


def touched_wide_upper(pools: list[Pool], obs: Observation, profile: CoinProfile) -> bool:
    """A single wide upper pool, not a stack of thin bands inside a range."""
    return any(
        pool.side == "upper"
        and pool.born <= obs.as_of
        and (pool.invalidated is None or pool.invalidated > obs.as_of)
        and pool.bottom <= obs.high * 1.005
        and pool.top >= obs.high * 0.995
        and _gap_pct(pool.bottom, pool.top) >= profile.relevant_pool_width_pct
        for pool in pools
    )


def bounce_pool_still_open(
    pools: list[Pool], obs: Observation, profile: CoinProfile, floor: float, floor_at: datetime | None
) -> bool:
    """A relevant lower pool was built after the low and is still waiting under price."""
    if floor <= 0 or floor_at is None:
        return False
    return any(
        pool.side == "lower"
        and floor_at < pool.born <= obs.as_of
        and (pool.invalidated is None or pool.invalidated > obs.as_of)
        and floor < pool.top < obs.price
        and pool.bottom >= floor * (1.0 + profile.cluster_gap_pct / 100.0)
        and _gap_pct(pool.bottom, pool.top) >= profile.relevant_pool_width_pct
        for pool in pools
    )


def lower_destination_open(
    pools: list[Pool], obs: Observation, profile: CoinProfile, floor: float | None = None
) -> bool:
    """A lower pool still sits at least one standalone gap under the stair floor."""
    anchor = floor if floor and floor > 0 else obs.price
    if anchor <= 0:
        return False
    limit = anchor * (1.0 - profile.standalone_gap_pct / 100.0)
    return any(
        pool.side == "lower"
        and pool.born <= obs.as_of
        and (pool.invalidated is None or pool.invalidated > obs.as_of)
        and pool.top <= limit
        for pool in pools
    )


def stair_step_count(
    pools: list[Pool],
    *,
    born_after: datetime,
    after: datetime,
    as_of: datetime,
    separation_pct: float,
) -> int:
    """Distinct lower levels created on the way up and liquidated on the way down."""
    tops = [
        pool.top
        for pool in pools
        if pool.side == "lower"
        and born_after <= pool.born <= as_of
        and pool.invalidated is not None
        and after < pool.invalidated <= as_of
    ]
    if not tops:
        return 0
    tops.sort(reverse=True)
    steps = 1
    last = tops[0]
    for top in tops[1:]:
        if _gap_pct(top, last) >= separation_pct:
            steps += 1
            last = top
    return steps

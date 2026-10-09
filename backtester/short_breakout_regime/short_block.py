"""Block shorts while price holds a 4h EMA-band breakout under thick pools.

Two checks only: the close came up through EMA9, EMA20, EMA59 and EMA200, and
a thick pool that already existed still reaches above the price. The block
stays through a dip under EMA9 or EMA20. It ends when a close is back under
EMA200, or when that overhead pool is gone.
This module does not place trades and is not imported by the frozen bounce runners.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


# Structure cuts already used by the pool backtester. They are not fitted
# to one DOGE swing. 0.25% merges nested slivers into one shelf. 0.8% is the
# same minimum room the bounce rule uses before the next pool.
BREAKOUT_MIN_PCT = 1.5
SHELF_MERGE_PCT = 0.25
GAP_BELOW_LEFTOVER_MIN_PCT = 0.8


@dataclass(frozen=True)
class PoolMark:
    bottom: float
    top: float
    strength: float
    born_ts: datetime


@dataclass(frozen=True)
class ShortBreakoutContext:
    """Inputs already known at the decision time. No lookahead fields."""

    price: float
    regime: str
    h1_label: str
    h4_label: str
    breakout_ts: datetime | None
    breakout_base: float | None
    ema9_held_since_breakout: bool
    ema20_held_since_breakout: bool
    crossed_from_below: bool
    pools: tuple[PoolMark, ...]


@dataclass(frozen=True)
class ShortBlockDecision:
    block_short: bool
    reason: str
    checks: dict[str, bool]


def _pct_gap(upper: float, lower: float) -> float:
    if lower <= 0:
        return 0.0
    return (upper - lower) / lower * 100.0


def breakout_up(ctx: ShortBreakoutContext) -> bool:
    if ctx.breakout_ts is None or ctx.breakout_base is None or ctx.breakout_base <= 0:
        return False
    return _pct_gap(ctx.price, ctx.breakout_base) >= BREAKOUT_MIN_PCT


def leftover_supports_below(ctx: ShortBreakoutContext) -> tuple[bool, PoolMark | None]:
    """The shelf left under the launch, not the newest pool under the last price."""
    if ctx.breakout_ts is None or ctx.breakout_base is None:
        return False, None
    leftovers = [
        pool
        for pool in ctx.pools
        if pool.top <= ctx.breakout_base
        and pool.top < ctx.price
        and pool.born_ts <= ctx.breakout_ts
    ]
    if not leftovers:
        return False, None
    shelf = max(leftovers, key=lambda pool: pool.top)
    return True, shelf


def _shelf_cluster(ctx: ShortBreakoutContext, shelf: PoolMark) -> list[PoolMark]:
    """Nested slivers under the launch count as one leftover zone."""
    members = [shelf]
    below = [
        pool
        for pool in ctx.pools
        if pool is not shelf and pool.top < ctx.price and pool.born_ts <= ctx.breakout_ts
    ]
    changed = True
    while changed:
        changed = False
        floor = min(pool.bottom for pool in members)
        for pool in below:
            if pool in members or pool.top >= floor:
                continue
            if _pct_gap(floor, pool.top) <= SHELF_MERGE_PCT:
                members.append(pool)
                changed = True
    return members


def gap_under_leftover(ctx: ShortBreakoutContext, leftover: PoolMark | None) -> bool:
    if leftover is None or ctx.breakout_ts is None:
        return False
    cluster = _shelf_cluster(ctx, leftover)
    floor = min(pool.bottom for pool in cluster)
    deeper = [pool for pool in ctx.pools if pool.top < floor]
    if not deeper:
        return True
    next_pool = max(deeper, key=lambda pool: pool.top)
    return _pct_gap(floor, next_pool.top) >= GAP_BELOW_LEFTOVER_MIN_PCT


def fresh_supports_only(ctx: ShortBreakoutContext) -> bool:
    """Supports born after the launch sit under price. They are not the origin."""
    if ctx.breakout_ts is None or ctx.breakout_base is None:
        return False
    return ctx.price > ctx.breakout_base


def old_upper_targets(ctx: ShortBreakoutContext) -> bool:
    """Pools above price that already existed when the launch candle opened.

    Age is the filter. A raw strength floor misses the 1h zones, whose
    recorded strength is often zero.
    """
    if ctx.breakout_ts is None or ctx.price <= 0:
        return False
    return any(
        pool.bottom > ctx.price and pool.born_ts <= ctx.breakout_ts for pool in ctx.pools
    )


def ema_bands(ctx: ShortBreakoutContext) -> bool:
    """Price closed up through the band from under EMA200 and still holds EMA200.

    A later close under EMA9 or EMA20 does not cancel the cross.
    """
    return ctx.crossed_from_below and ctx.ema9_held_since_breakout and ctx.ema20_held_since_breakout


def thick_pools_above(ctx: ShortBreakoutContext) -> bool:
    """Wide pools already sitting above the price. Thin lines do not count."""
    if ctx.price <= 0:
        return False
    above = [
        pool
        for pool in ctx.pools
        if pool.top > ctx.price and (ctx.breakout_ts is None or pool.born_ts <= ctx.breakout_ts)
    ]
    if not above:
        return False
    widths = sorted((pool.top - pool.bottom) / ctx.price * 100.0 for pool in above)
    if len(widths) == 1:
        return widths[0] > 0
    # Lower of the two middle widths, so the widest pool is compared with the
    # others and not with itself when only two pools sit above price.
    median = widths[(len(widths) - 1) // 2]
    if median <= 0:
        return False
    return any(width >= 2.0 * median for width in widths)


def should_block_short(ctx: ShortBreakoutContext) -> ShortBlockDecision:
    """Short block uses only the EMA bands and thick pools above price."""
    checks = {
        "ema_bands": ema_bands(ctx),
        "thick_pools_above": thick_pools_above(ctx),
    }
    if all(checks.values()):
        return ShortBlockDecision(True, "ema_bands_and_thick_pools_above", checks)
    missing = [name for name, ok in checks.items() if not ok]
    return ShortBlockDecision(False, "missing_" + "+".join(missing), checks)

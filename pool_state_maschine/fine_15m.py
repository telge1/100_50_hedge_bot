"""15m timing on top of a finished 4h phase. Does not change the 4h machine.

The 4h phase stays the decision. 15m only picks the bar inside that 4h candle
where price actually meets the pool the phase is about.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pool_state_maschine.market_data import Bar, Pool, _paths, _utc
from pool_state_maschine.scanner import request_for, walk
from pool_state_maschine.state_machine import MarketPhase, MarketState


def load_bars(symbol: str, timeframe: str, start: datetime, end: datetime) -> list[Bar]:
    _paths()
    from dashboard.research_charts.service import _candles_from_packed, load_candles

    packed = load_candles(
        symbol,
        timeframe,
        start=int(start.timestamp()),
        end=int(end.timestamp()),
        limit=50000,
    )
    candles = _candles_from_packed(packed)
    return [
        Bar(
            ts=_utc(c.timestamp),
            open=float(c.open),
            high=float(c.high),
            low=float(c.low),
            close=float(c.close),
        )
        for c in candles
    ]


def _switches(states: list[MarketState]) -> list[tuple[MarketState, MarketState]]:
    changes: list[tuple[MarketState, MarketState]] = []
    previous = states[0]
    for state in states[1:]:
        if state.phase != previous.phase and state.as_of is not None and previous.as_of is not None:
            changes.append((previous, state))
        previous = state
    return changes


def _active(pools: list[Pool], moment: datetime, side: str) -> list[Pool]:
    return [
        pool
        for pool in pools
        if pool.side == side
        and pool.born <= moment
        and (pool.invalidated is None or pool.invalidated > moment)
    ]


def _close_at(bar: Bar) -> datetime:
    return bar.ts + timedelta(minutes=15)


def _refine_up(bars: list[Bar], pools: list[Pool], moment: datetime) -> datetime | None:
    """Last 15m touch of the held lower pool, then the close that leaves it."""
    window = [bar for bar in bars if moment - timedelta(days=3) < _close_at(bar) <= moment]
    if not window:
        return None
    floor = min(bar.low for bar in window)
    zone = [
        pool
        for pool in _active(pools, moment, "lower")
        if pool.top >= floor and pool.bottom <= floor * 1.03
    ]
    if not zone:
        return None
    edge = max(pool.top for pool in zone)
    last_touch = None
    for bar in window:
        if bar.low <= edge:
            last_touch = bar
    if last_touch is None:
        return None
    for bar in window:
        if bar.ts < last_touch.ts:
            continue
        if bar.close > edge:
            return _close_at(bar)
    return _close_at(last_touch)


def _refine_down(bars: list[Bar], pools: list[Pool], moment: datetime) -> datetime | None:
    """Last 15m tag of the upper pool, then the close back underneath it."""
    window = [bar for bar in bars if moment - timedelta(days=6) < _close_at(bar) <= moment]
    if not window:
        return None
    ceiling = max(bar.high for bar in window)
    zones = [
        pool
        for pool in _active(pools, moment, "upper")
        if pool.bottom <= ceiling <= pool.top * 1.002
        and (pool.top - pool.bottom) / pool.bottom >= 0.025
    ]
    if not zones:
        return None
    zone = min(zones, key=lambda pool: abs(pool.bottom - ceiling))
    last_tag = None
    for bar in window:
        if bar.high >= zone.bottom:
            last_tag = bar
    if last_tag is None:
        return None
    for bar in window:
        if bar.ts < last_tag.ts:
            continue
        if bar.close < zone.bottom:
            return _close_at(bar)
    return _close_at(last_tag)


def refine(symbol: str, *, since: datetime) -> list[tuple[datetime, str, datetime | None]]:
    as_of = datetime(2026, 9, 27, tzinfo=timezone.utc)
    states = walk(request_for(symbol, lookback_days=180, as_of=as_of))
    from pool_state_maschine.market_data import load_market

    _, pools = load_market(symbol, as_of - timedelta(days=225), as_of)
    bars = load_bars(symbol, "15m", since - timedelta(days=2), as_of)
    out: list[tuple[datetime, str, datetime | None]] = []
    for previous, state in _switches(states):
        if state.as_of is None or state.as_of < since:
            continue
        turning_up = state.phase in {MarketPhase.EXHAUSTED, MarketPhase.UPPER_BUILD} and previous.phase in {
            MarketPhase.STAIR_DOWN,
            MarketPhase.EXHAUSTED,
        }
        if turning_up and previous.phase is not state.phase:
            out.append((state.as_of, "up", _refine_up(bars, pools, state.as_of)))
        elif state.phase is MarketPhase.STAIR_DOWN and previous.phase is MarketPhase.UPPER_BUILD:
            out.append((state.as_of, "down", _refine_down(bars, pools, state.as_of)))
    return out


def main() -> None:
    since = datetime(2026, 6, 1, tzinfo=timezone.utc)
    for symbol in ("ADAUSDT", "DOGEUSDT"):
        print(symbol)
        for coarse, direction, fine in refine(symbol, since=since):
            shifted = ""
            if fine is not None and fine != coarse:
                minutes = int((coarse - fine).total_seconds() // 60)
                shifted = f"  {minutes}m earlier"
            print(f"  {coarse.isoformat()} {direction:4} -> {fine.isoformat() if fine else 'none'}{shifted}")


if __name__ == "__main__":
    main()

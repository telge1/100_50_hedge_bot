"""How far price ran for and against a signal, on the 15m path.

The window is from the 4h close until the next signal. Entry is that close.
MFE is the furthest favorable print. MAE is the furthest adverse print.
The third number is the adverse print before the first close beyond entry.
It does not change the phase and does not place an order. Fees are not applied.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pool_pattern.market import Bar
from pool_pattern.signal import Signal, SignalStamp


@dataclass(frozen=True)
class Excursion:
    as_of: datetime
    signal: Signal
    entry: float
    until: datetime | None
    mfe_pct: float
    mae_pct: float
    mae_before_profit_pct: float | None


def measure(signals: list[SignalStamp], bars: list[Bar], entry_at: dict[datetime, float]) -> list[Excursion]:
    ordered = [item for item in signals if item.signal is not Signal.NONE]
    path = sorted(bars, key=lambda bar: bar.ts)
    out: list[Excursion] = []
    for index, item in enumerate(ordered):
        until = ordered[index + 1].as_of if index + 1 < len(ordered) else None
        entry = entry_at[item.as_of]
        window = [bar for bar in path if bar.ts >= item.as_of and (until is None or bar.ts < until)]
        out.append(_walk(item, entry, until, window))
    return out


def _walk(item: SignalStamp, entry: float, until: datetime | None, window: list[Bar]) -> Excursion:
    best = 0.0
    worst = 0.0
    before = 0.0
    in_profit = False
    for bar in window:
        adverse, favorable_close, favorable_extreme = _moves(item.signal, entry, bar)
        if favorable_extreme > best:
            best = favorable_extreme
        if adverse > worst:
            worst = adverse
        if not in_profit:
            if adverse > before:
                before = adverse
            if favorable_close > 0.0:
                in_profit = True
    return Excursion(
        item.as_of,
        item.signal,
        entry,
        until,
        best,
        worst,
        before if in_profit else None,
    )


def _moves(signal: Signal, entry: float, bar: Bar) -> tuple[float, float, float]:
    if signal is Signal.LONG:
        return (
            _pct(entry, entry - bar.low),
            _pct(entry, bar.close - entry),
            _pct(entry, bar.high - entry),
        )
    return (
        _pct(entry, bar.high - entry),
        _pct(entry, entry - bar.close),
        _pct(entry, entry - bar.low),
    )


def _pct(entry: float, move: float) -> float:
    if entry <= 0.0:
        return 0.0
    return move / entry * 100.0

"""Confirmation after a 4h phase change.

The 4h phase stays as it is. 15m and 1h only say whether that change held
through the next four hours. Neither starts a phase or emits a signal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from pool_pattern.machine import Phase, PhaseStamp
from pool_pattern.market import Bar


@dataclass(frozen=True)
class ConfirmedStamp:
    stamp: PhaseStamp
    confirmed: bool | None
    note: str


def confirm_switch(
    stamp: PhaseStamp,
    bars_15m: list[Bar],
    *,
    switched: bool,
    span: timedelta | None = None,
) -> ConfirmedStamp:
    if not switched:
        return ConfirmedStamp(stamp, None, "no_switch")
    window = _after(stamp, bars_15m, span or _bar_span(bars_15m))
    if len(window) < 4:
        return ConfirmedStamp(stamp, None, "no_15m")
    if stamp.phase is Phase.STAIR_DOWN:
        if stamp.upper_low is None:
            return ConfirmedStamp(stamp, None, "no_upper_level")
        held = all(bar.close < stamp.upper_low for bar in window)
        return ConfirmedStamp(stamp, held, "held_under_upper" if held else "reclaimed_upper")
    if stamp.phase is Phase.UPPER_BUILD:
        if stamp.lower_high is None:
            return ConfirmedStamp(stamp, None, "no_lower_level")
        held = window[-1].close > stamp.lower_high
        return ConfirmedStamp(stamp, held, "held_above_lower" if held else "lost_lower_zone")
    if stamp.phase is Phase.EXHAUSTED:
        if stamp.lower_high is None:
            return ConfirmedStamp(stamp, None, "no_lower_level")
        tagged = any(bar.low <= stamp.lower_high for bar in window)
        held = window[-1].close > stamp.lower_high
        ok = tagged and held
        return ConfirmedStamp(stamp, ok, "lower_zone_held" if ok else "lower_zone_not_held")
    return ConfirmedStamp(stamp, None, "no_switch")


def annotate(stamps: list[PhaseStamp], bars: list[Bar]) -> list[ConfirmedStamp]:
    span = _bar_span(bars)
    out: list[ConfirmedStamp] = []
    previous = None
    for stamp in stamps:
        switched = previous is not None and stamp.phase is not previous
        if switched:
            out.append(confirm_switch(stamp, bars, switched=True, span=span))
        elif _took_upper(stamp, bars, span):
            out.append(_confirm_taken_upper(stamp, bars, span))
        else:
            out.append(confirm_switch(stamp, bars, switched=False, span=span))
        previous = stamp.phase
    return out


def _took_upper(stamp: PhaseStamp, bars: list[Bar], span: timedelta) -> bool:
    """The 4h candle traded the tagged upper and closed back under it."""
    if stamp.phase is not Phase.STAIR_DOWN or stamp.upper_low is None:
        return False
    inside = _between(stamp.as_of - timedelta(hours=4), stamp.as_of, bars, span)
    if not inside:
        return False
    return any(bar.high >= stamp.upper_low for bar in inside) and inside[-1].close < stamp.upper_low


def _confirm_taken_upper(stamp: PhaseStamp, bars: list[Bar], span: timedelta) -> ConfirmedStamp:
    window = _after(stamp, bars, span)
    if len(window) < 4:
        return ConfirmedStamp(stamp, None, "no_15m")
    held = all(bar.close < stamp.upper_low for bar in window)
    return ConfirmedStamp(stamp, held, "held_under_taken_upper" if held else "reclaimed_upper")


def _after(stamp: PhaseStamp, bars: list[Bar], span: timedelta) -> list[Bar]:
    return _between(stamp.as_of, stamp.as_of + timedelta(hours=4), bars, span)


def _between(start, end, bars: list[Bar], span: timedelta) -> list[Bar]:
    return [bar for bar in bars if start < bar.ts + span <= end]


def _bar_span(bars: list[Bar]) -> timedelta:
    gaps = sorted((right.ts - left.ts for left, right in zip(bars, bars[1:]) if right.ts > left.ts))
    if not gaps:
        return timedelta(minutes=15)
    return gaps[len(gaps) // 2]

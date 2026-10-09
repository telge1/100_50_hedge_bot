"""Long and short only from a confirmed stair.

4h sets the phase. A short needs stair_down confirmed on 15m and 1h.
A long needs upper_build confirmed on both. Anything else is no trade.
This module does not change the phase and does not place an order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from pool_pattern.confirm_15m import annotate
from pool_pattern.machine import Phase, PhaseStamp
from pool_pattern.market import Bar


class Signal(str, Enum):
    LONG = "long"
    SHORT = "short"
    NONE = "none"


@dataclass(frozen=True)
class SignalStamp:
    as_of: datetime
    signal: Signal
    phase: Phase
    reason: str


def read_signals(stamps: list[PhaseStamp], bars_15m: list[Bar], bars_1h: list[Bar]) -> list[SignalStamp]:
    """A signal is only the 4h phase change, confirmed by 15m and 1h.

    A later touch of an upper pool inside a stair that is already running is not a new short.
    """
    fine = annotate(stamps, bars_15m)
    hour = annotate(stamps, bars_1h)
    out: list[SignalStamp] = []
    previous: Phase | None = None
    for stamp, on_15m, on_1h in zip(stamps, fine, hour):
        switched = previous is not None and stamp.phase is not previous
        both = on_15m.confirmed is True and on_1h.confirmed is True
        signal, reason = _signal(stamp, switched, both)
        out.append(SignalStamp(stamp.as_of, signal, stamp.phase, reason))
        previous = stamp.phase
    return out


def _signal(stamp: PhaseStamp, switched: bool, confirmed: bool) -> tuple[Signal, str]:
    if not switched or not confirmed:
        return Signal.NONE, "not_confirmed"
    if stamp.phase is Phase.STAIR_DOWN:
        return Signal.SHORT, "confirmed_stair"
    if stamp.phase is Phase.UPPER_BUILD:
        return Signal.LONG, "confirmed_upper"
    return Signal.NONE, "no_trade"

from __future__ import annotations

from ob_microstructure_breakout_bot.models import EmaSnapshot, TouchDirection


def ema_stack_ok(ema: EmaSnapshot | None, direction: TouchDirection) -> bool:
    """V2 filter.

    Longs require EMA9 and EMA20 to be above EMA59.
    Shorts require EMA9 and EMA20 to be below EMA59.
    """
    if ema is None:
        return False

    long_ok = ema.ema9 > ema.ema59 and ema.ema20 > ema.ema59 and ema.price > ema.ema59
    short_ok = ema.ema9 < ema.ema59 and ema.ema20 < ema.ema59 and ema.price < ema.ema59

    if direction == TouchDirection.FROM_BELOW:
        return long_ok
    if direction == TouchDirection.FROM_ABOVE:
        return short_ok
    return False


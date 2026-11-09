from __future__ import annotations

from ob_microstructure_breakout_bot.calibration_v2.ema_stack_filters import ema_stack_ok
from ob_microstructure_breakout_bot.models import EmaSnapshot, TouchDirection


def test_long_requires_ema9_and_ema20_above_ema59() -> None:
    ema = EmaSnapshot(ema9=1.05, ema20=1.02, ema59=1.0, price=1.03, ema200=None)
    assert ema_stack_ok(ema, TouchDirection.FROM_BELOW)


def test_long_does_not_require_ema9_above_ema20() -> None:
    ema = EmaSnapshot(ema9=1.03, ema20=1.05, ema59=1.0, price=1.04, ema200=None)
    assert ema_stack_ok(ema, TouchDirection.FROM_BELOW)


def test_long_rejects_bearish_stack() -> None:
    ema = EmaSnapshot(ema9=1.05, ema20=1.02, ema59=1.08, price=1.07, ema200=None)
    assert not ema_stack_ok(ema, TouchDirection.FROM_BELOW)


def test_short_requires_ema9_and_ema20_below_ema59() -> None:
    ema = EmaSnapshot(ema9=0.98, ema20=0.99, ema59=1.0, price=0.97, ema200=None)
    assert ema_stack_ok(ema, TouchDirection.FROM_ABOVE)


def test_short_does_not_require_ema9_below_ema20() -> None:
    ema = EmaSnapshot(ema9=0.99, ema20=0.97, ema59=1.0, price=0.98, ema200=None)
    assert ema_stack_ok(ema, TouchDirection.FROM_ABOVE)


def test_short_rejects_bullish_stack() -> None:
    ema = EmaSnapshot(ema9=1.02, ema20=1.01, ema59=1.0, price=1.03, ema200=None)
    assert not ema_stack_ok(ema, TouchDirection.FROM_ABOVE)


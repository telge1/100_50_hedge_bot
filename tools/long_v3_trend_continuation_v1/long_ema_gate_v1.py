"""LONG V3 EMA permission gate (closed 5m bar only)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LongEmaGateResultV1:
    authorized: bool
    reason_code: str
    ema9: float | None
    ema20: float | None
    ema59: float | None
    ema200: float | None
    ema200_prev6: float | None
    audit: dict[str, Any]


def evaluate_long_ema_gate(
    *,
    ema9: float | None,
    ema20: float | None,
    ema59: float | None,
    ema200: float | None,
    ema200_prev6: float | None,
) -> LongEmaGateResultV1:
    if None in (ema9, ema20, ema59, ema200, ema200_prev6):
        return LongEmaGateResultV1(
            authorized=False,
            reason_code="EMA_NOT_READY",
            ema9=ema9,
            ema20=ema20,
            ema59=ema59,
            ema200=ema200,
            ema200_prev6=ema200_prev6,
            audit={},
        )
    checks = {
        "ema9_above_200": ema9 > ema200,
        "ema20_above_200": ema20 > ema200,
        "ema59_above_200": ema59 > ema200,
        "ema200_rising_6b": ema200 > ema200_prev6,
    }
    if not all(checks.values()):
        code = next(k for k, v in checks.items() if not v)
        return LongEmaGateResultV1(
            authorized=False,
            reason_code=code.upper(),
            ema9=ema9,
            ema20=ema20,
            ema59=ema59,
            ema200=ema200,
            ema200_prev6=ema200_prev6,
            audit=checks,
        )
    return LongEmaGateResultV1(
        authorized=True,
        reason_code="LONG_EMA_OK",
        ema9=ema9,
        ema20=ema20,
        ema59=ema59,
        ema200=ema200,
        ema200_prev6=ema200_prev6,
        audit=checks,
    )

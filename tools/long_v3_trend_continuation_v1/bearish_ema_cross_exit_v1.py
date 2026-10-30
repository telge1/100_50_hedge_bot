"""Bearish EMA59/200 cross on closed 5m bars (LONG B exit)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BearishEmaCrossSignalV1:
    bar_utc: str
    ema_fast: float
    ema_slow: float
    prev_bar_utc: str


def bearish_cross_confirmed_on_close(
    *,
    prev_bar_utc: str,
    bar_utc: str,
    prev_ema_fast: float,
    prev_ema_slow: float,
    ema_fast: float,
    ema_slow: float,
) -> BearishEmaCrossSignalV1 | None:
    """EMA59 crosses below EMA200: prev fast >= slow and current fast < slow."""
    if prev_ema_fast < prev_ema_slow - 1e-15:
        return None
    if ema_fast >= ema_slow - 1e-15:
        return None
    return BearishEmaCrossSignalV1(
        bar_utc=bar_utc,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        prev_bar_utc=prev_bar_utc,
    )

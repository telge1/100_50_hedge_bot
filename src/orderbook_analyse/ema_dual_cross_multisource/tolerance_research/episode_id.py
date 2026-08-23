"""Stable cross_episode_id linking the same underlying EMA event across modes."""

from __future__ import annotations

import hashlib
from typing import Any


def make_cross_episode_id(
    *,
    symbol: str,
    timeframe: str,
    direction: str,
    first_cross_bar: int,
    first_leg: str,
) -> str:
    """
    Episode boundary: one contiguous dual-cross sequence in one direction.

    - first_cross_bar: bar index of the first confirming leg (or both for gap-0)
    - first_leg: EMA9 | EMA20 | BOTH
    Same first bar + first leg + direction → same episode across M0/M1 gap caps.
    """
    key = (
        f"{str(symbol).upper()}|{str(timeframe)}|{str(direction).upper()}|"
        f"{int(first_cross_bar)}|{str(first_leg).upper()}"
    )
    return "edx:" + hashlib.sha1(key.encode()).hexdigest()[:20]


def episode_fields_from_candidate(raw: dict[str, Any]) -> dict[str, Any]:
    metrics = raw.get("ema_metrics") or {}
    first_bar = metrics.get("first_cross_bar")
    if first_bar is None:
        first_bar = raw.get("bar_index")
    first_leg = metrics.get("first_leg") or ("BOTH" if int(metrics.get("exact_gap") or 0) == 0 else "UNKNOWN")
    return {
        "cross_episode_id": make_cross_episode_id(
            symbol=str(raw["symbol"]),
            timeframe=str(raw["timeframe"]),
            direction=str(raw["direction"]),
            first_cross_bar=int(first_bar),
            first_leg=str(first_leg),
        ),
        "first_cross_bar": int(first_bar),
        "first_leg": str(first_leg),
        "exact_gap": int(metrics.get("exact_gap") or 0),
    }

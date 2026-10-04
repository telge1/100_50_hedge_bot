"""Causal m15 lower-ladder age at entry (frozen filter feature)."""

from __future__ import annotations

from datetime import datetime


def pool_age_hours(moment: datetime, known: datetime) -> float:
    return (moment - known).total_seconds() / 3600.0


def _active_pools(pools: list[dict], moment: datetime, side: str) -> list[dict]:
    out = []
    for p in pools:
        if p.get("side") != side:
            continue
        if p["known"] > moment:
            continue
        br = p.get("break_at")
        if br is not None and br <= moment:
            continue
        out.append(p)
    return out


def lowers_under_price(pools: list[dict], moment: datetime, price: float) -> list[dict]:
    return [p for p in _active_pools(pools, moment, "lower") if float(p["top"]) < price]


def m15_lower_2_age_h(pools: list[dict], moment: datetime, entry_price: float) -> float | None:
    lowers = sorted(lowers_under_price(pools, moment, entry_price), key=lambda p: -p["top"])
    if len(lowers) < 2:
        return None
    return round(pool_age_hours(moment, lowers[1]["known"]), 4)


def passes_ladder_filter(age_h: float | None) -> bool:
    if age_h is None:
        return False
    from . import config as C

    return age_h <= C.LADDER_MAX_AGE_H

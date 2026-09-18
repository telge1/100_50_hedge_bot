"""UPPER/LOWER zone geometry and normalized price bands."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ZoneBands:
    role: str  # UPPER | LOWER
    low: float
    high: float
    center: float
    defense_side: str  # ask for UPPER, bid for LOWER
    attack_side: str
    # absolute price intervals (inclusive edges for depth sums)
    inside: tuple[float, float]
    before_1bp: tuple[float, float]
    before_2bps: tuple[float, float]
    before_5bps: tuple[float, float]
    beyond_1bp: tuple[float, float]
    beyond_2bps: tuple[float, float]
    beyond_5bps: tuple[float, float]
    opposing_1bp: tuple[float, float]
    opposing_2bps: tuple[float, float]
    opposing_5bps: tuple[float, float]
    query_min: float
    query_max: float


def _bp(ref: float, bps: float) -> float:
    return abs(float(ref)) * float(bps) / 10_000.0


def build_zone_bands(*, role: str, low: float, high: float) -> ZoneBands:
    role = str(role).upper()
    lo = float(min(low, high))
    hi = float(max(low, high))
    center = (lo + hi) / 2.0 if hi > lo else lo
    if role == "UPPER":
        # attack from below; beyond = above; defense = ask
        defense, attack = "ask", "bid"
        before_1 = (lo - _bp(center, 1.0), lo)
        before_2 = (lo - _bp(center, 2.0), lo - _bp(center, 1.0))
        before_5 = (lo - _bp(center, 5.0), lo - _bp(center, 2.0))
        beyond_1 = (hi, hi + _bp(center, 1.0))
        beyond_2 = (hi + _bp(center, 1.0), hi + _bp(center, 2.0))
        beyond_5 = (hi + _bp(center, 2.0), hi + _bp(center, 5.0))
        # opposing = bid near zone from below
        opp_1 = (lo - _bp(center, 1.0), lo)
        opp_2 = (lo - _bp(center, 2.0), lo - _bp(center, 1.0))
        opp_5 = (lo - _bp(center, 5.0), lo - _bp(center, 2.0))
        qmin = lo - _bp(center, 5.0) - _bp(center, 1.0)
        qmax = hi + _bp(center, 5.0) + _bp(center, 1.0)
    elif role == "LOWER":
        # attack from above; beyond = below; defense = bid
        defense, attack = "bid", "ask"
        before_1 = (hi, hi + _bp(center, 1.0))
        before_2 = (hi + _bp(center, 1.0), hi + _bp(center, 2.0))
        before_5 = (hi + _bp(center, 2.0), hi + _bp(center, 5.0))
        beyond_1 = (lo - _bp(center, 1.0), lo)
        beyond_2 = (lo - _bp(center, 2.0), lo - _bp(center, 1.0))
        beyond_5 = (lo - _bp(center, 5.0), lo - _bp(center, 2.0))
        opp_1 = (hi, hi + _bp(center, 1.0))
        opp_2 = (hi + _bp(center, 1.0), hi + _bp(center, 2.0))
        opp_5 = (hi + _bp(center, 2.0), hi + _bp(center, 5.0))
        qmin = lo - _bp(center, 5.0) - _bp(center, 1.0)
        qmax = hi + _bp(center, 5.0) + _bp(center, 1.0)
    else:
        raise ValueError(f"unknown role {role}")
    return ZoneBands(
        role=role,
        low=lo,
        high=hi,
        center=center,
        defense_side=defense,
        attack_side=attack,
        inside=(lo, hi),
        before_1bp=before_1,
        before_2bps=before_2,
        before_5bps=before_5,
        beyond_1bp=beyond_1,
        beyond_2bps=beyond_2,
        beyond_5bps=beyond_5,
        opposing_1bp=opp_1,
        opposing_2bps=opp_2,
        opposing_5bps=opp_5,
        query_min=qmin,
        query_max=qmax,
    )


def price_in_interval(price: float, interval: tuple[float, float]) -> bool:
    a, b = interval
    lo, hi = (a, b) if a <= b else (b, a)
    # zero-width zone: treat exact price as inside
    if abs(hi - lo) < 1e-12:
        return abs(float(price) - lo) <= 1e-9
    return lo - 1e-12 <= float(price) <= hi + 1e-12


def sum_depth(
    sizes: dict[tuple[str, float], float],
    *,
    side: str,
    interval: tuple[float, float],
) -> float:
    total = 0.0
    for (s, px), sz in sizes.items():
        if s != side:
            continue
        if sz <= 0:
            continue
        if price_in_interval(px, interval):
            total += float(sz)
    return total


def notional(price: float, qty: float) -> float:
    return float(price) * float(qty)

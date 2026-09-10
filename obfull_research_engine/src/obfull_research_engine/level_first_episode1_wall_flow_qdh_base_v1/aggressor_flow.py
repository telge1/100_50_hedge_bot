"""Aggressor flow intensity and persistence (research defaults, outcome-blind)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import EPSILON, FAST_HALF_LIFE_MS, SLOW_HALF_LIFE_MS


def ewma_update(prev: float, value: float, *, dt_s: float, half_life_s: float) -> float:
    if dt_s < 0:
        raise ValueError("dt_s must be non-negative")
    if half_life_s <= 0:
        raise ValueError("half_life_s must be positive")
    if dt_s == 0:
        return value if prev == 0 else (0.5 * prev + 0.5 * value)
    alpha = 1.0 - math.exp(-math.log(2.0) * dt_s / half_life_s)
    return (1.0 - alpha) * prev + alpha * value


def clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


@dataclass
class AggressorState:
    hit_rate_qty_per_s: float = 0.0
    hit_rate_notional_per_s: float = 0.0
    trade_rate_per_s: float = 0.0
    fast_hit_rate: float = 0.0
    slow_hit_rate: float = 0.0
    persistence_ratio: float = 1.0
    interarrival_p50_ms: float | None = None
    interarrival_p90_ms: float | None = None
    same_side_trade_ratio: float = 1.0
    time_since_last_hit_ms: float | None = None
    last_hit_exchange_time: datetime | None = None
    cumulative_hit_qty: float = 0.0
    cumulative_hit_notional: float = 0.0
    cumulative_hit_trades: int = 0


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def update_aggressor(
    state: AggressorState,
    *,
    hit_qty: float,
    hit_notional: float,
    hit_trade_count: int,
    interval_duration_s: float,
    exchange_time: datetime,
    interarrival_ms_samples: list[float],
    buy_hit_qty: float,
    sell_hit_qty: float,
    fast_half_life_ms: float = FAST_HALF_LIFE_MS,
    slow_half_life_ms: float = SLOW_HALF_LIFE_MS,
) -> AggressorState:
    dur = max(float(interval_duration_s), EPSILON)
    hit_rate = float(hit_qty) / dur
    notional_rate = float(hit_notional) / dur
    trade_rate = float(hit_trade_count) / dur
    dt = dur
    fast = ewma_update(state.fast_hit_rate, hit_rate, dt_s=dt, half_life_s=fast_half_life_ms / 1000.0)
    slow = ewma_update(state.slow_hit_rate, hit_rate, dt_s=dt, half_life_s=slow_half_life_ms / 1000.0)
    persistence = fast / (slow + EPSILON)
    total_side = buy_hit_qty + sell_hit_qty
    same_side = (buy_hit_qty / total_side) if total_side > EPSILON else 1.0
    samples = sorted(interarrival_ms_samples)
    tsl = None
    if state.last_hit_exchange_time is not None:
        tsl = (exchange_time - state.last_hit_exchange_time).total_seconds() * 1000.0
    last = state.last_hit_exchange_time
    if hit_trade_count > 0:
        last = exchange_time
        tsl = 0.0
    return AggressorState(
        hit_rate_qty_per_s=hit_rate,
        hit_rate_notional_per_s=notional_rate,
        trade_rate_per_s=trade_rate,
        fast_hit_rate=fast,
        slow_hit_rate=slow,
        persistence_ratio=persistence,
        interarrival_p50_ms=_percentile(samples, 0.50),
        interarrival_p90_ms=_percentile(samples, 0.90),
        same_side_trade_ratio=same_side,
        time_since_last_hit_ms=tsl,
        last_hit_exchange_time=last,
        cumulative_hit_qty=state.cumulative_hit_qty + hit_qty,
        cumulative_hit_notional=state.cumulative_hit_notional + hit_notional,
        cumulative_hit_trades=state.cumulative_hit_trades + hit_trade_count,
    )


def aggressor_to_dict(state: AggressorState) -> dict[str, Any]:
    return {
        "hit_rate_qty_per_s": state.hit_rate_qty_per_s,
        "hit_rate_notional_per_s": state.hit_rate_notional_per_s,
        "trade_rate_per_s": state.trade_rate_per_s,
        "fast_hit_rate": state.fast_hit_rate,
        "slow_hit_rate": state.slow_hit_rate,
        "persistence_ratio": state.persistence_ratio,
        "interarrival_p50_ms": state.interarrival_p50_ms,
        "interarrival_p90_ms": state.interarrival_p90_ms,
        "same_side_trade_ratio": state.same_side_trade_ratio,
        "time_since_last_hit_ms": state.time_since_last_hit_ms,
        "cumulative_hit_qty": state.cumulative_hit_qty,
        "cumulative_hit_notional": state.cumulative_hit_notional,
        "cumulative_hit_trades": state.cumulative_hit_trades,
        "fast_half_life_ms_research_default": FAST_HALF_LIFE_MS,
        "slow_half_life_ms_research_default": SLOW_HALF_LIFE_MS,
    }

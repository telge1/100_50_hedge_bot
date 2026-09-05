"""Causal structure break using only past closed 1s buckets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from orderbook_analyse.aggressor_efficiency_flip.contracts import AEFConfig
from orderbook_analyse.aggressor_efficiency_flip.models import SecondBucket
from orderbook_analyse.aggressor_efficiency_flip.timeutil import floor_second


@dataclass
class StructureBreak:
    found: bool
    level: Optional[float]
    break_ts: Optional[datetime]  # bucket start that broke
    confirmed_ts: Optional[datetime]  # bucket close
    reason_code: str


def frozen_structure_level(
    buckets: dict[datetime, SecondBucket],
    *,
    as_of_exclusive: datetime,
    direction: str,
    lookback_s: int,
) -> Optional[float]:
    """Level frozen at flip decision: rolling max high (LONG) / min low (SHORT)."""
    end = floor_second(as_of_exclusive)
    start = end - timedelta(seconds=int(lookback_s))
    vals: list[float] = []
    cur = start
    while cur < end:
        b = buckets.get(cur)
        if b is not None:
            if direction == "LONG" and b.high_price is not None:
                vals.append(b.high_price)
            if direction == "SHORT" and b.low_price is not None:
                vals.append(b.low_price)
        cur += timedelta(seconds=1)
    if not vals:
        return None
    return max(vals) if direction == "LONG" else min(vals)


def find_structure_break(
    buckets: dict[datetime, SecondBucket],
    *,
    search_start: datetime,
    search_end: datetime,
    direction: str,
    level: float,
    cfg: AEFConfig,
) -> StructureBreak:
    """Break confirmed at close of first 1s bucket that pierces level by eps."""
    cur = floor_second(search_start)
    end = floor_second(search_end)
    eps = float(cfg.structure_break_eps_bps) / 10_000.0
    while cur < end:
        b = buckets.get(cur)
        close_ts = cur + timedelta(seconds=1)
        if b is not None and b.last_price is not None:
            px = b.last_price
            if direction == "LONG":
                thr = level * (1.0 + eps)
                hit = (b.high_price or px) >= thr
            else:
                thr = level * (1.0 - eps)
                hit = (b.low_price or px) <= thr
            if hit:
                return StructureBreak(True, level, cur, close_ts, "structure_break")
        cur += timedelta(seconds=1)
    return StructureBreak(False, level, None, None, "no_structure_break")

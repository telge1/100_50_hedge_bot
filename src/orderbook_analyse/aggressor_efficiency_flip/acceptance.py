"""Causal acceptance after structure break."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from orderbook_analyse.aggressor_efficiency_flip.contracts import AEFConfig
from orderbook_analyse.aggressor_efficiency_flip.models import SecondBucket
from orderbook_analyse.aggressor_efficiency_flip.timeutil import floor_second


@dataclass
class Acceptance:
    found: bool
    confirmed_ts: Optional[datetime]
    hold_seconds: int
    max_reclaim_bps: float
    reason_code: str


def find_acceptance(
    buckets: dict[datetime, SecondBucket],
    *,
    break_confirmed_ts: datetime,
    search_end: datetime,
    direction: str,
    level: float,
    cfg: AEFConfig,
) -> Acceptance:
    """Acceptance only after break_confirmed_ts; timestamp is when hold completes."""
    cur = floor_second(break_confirmed_ts)
    end = floor_second(search_end)
    need = int(cfg.acceptance_hold_seconds)
    held = 0
    max_reclaim = 0.0
    while cur < end:
        b = buckets.get(cur)
        close_ts = cur + timedelta(seconds=1)
        if b is None or b.last_price is None:
            held = 0
            cur += timedelta(seconds=1)
            continue
        px = b.last_price
        if direction == "LONG":
            on_side = px >= level
            reclaim = max(0.0, (level - (b.low_price or px)) / level * 10_000.0)
        else:
            on_side = px <= level
            reclaim = max(0.0, ((b.high_price or px) - level) / level * 10_000.0)
        max_reclaim = max(max_reclaim, reclaim)
        if on_side and reclaim <= cfg.acceptance_max_reclaim_bps:
            held += 1
            if held >= need:
                return Acceptance(True, close_ts, held, max_reclaim, "acceptance_confirmed")
        else:
            held = 0
            if reclaim > cfg.acceptance_max_reclaim_bps * 2:
                return Acceptance(False, None, held, max_reclaim, "failed_reclaim")
        cur += timedelta(seconds=1)
    return Acceptance(False, None, held, max_reclaim, "acceptance_timeout")

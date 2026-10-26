"""Pending limit fills and SL/TP exits (config-driven)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ClosedTradeV1:
    trade_id: str
    signal_close_utc: str
    fill_utc: str
    exit_utc: str
    entry_price: float
    exit_price: float
    exit_reason: str
    direction: str
    bars_held: int
    ambiguous_intrabar: bool
    sl_price: float
    tp_price: float


def try_pending_fill(
    entry_level: float,
    order_active_from_utc: str,
    prices: dict[str, dict],
    pilot_to: str,
) -> tuple[str | None, float | None]:
    times = sorted(t for t in prices if t >= order_active_from_utc and t <= pilot_to)
    for t in times:
        bar = prices[t]
        if bar["l"] <= entry_level <= bar["h"]:
            return t, entry_level
    return None, None


def simulate_short_exit(
    config: dict[str, Any],
    *,
    entry_utc: str,
    entry_price: float,
    sl_price: float,
    tp_price: float,
    prices: dict[str, dict],
    end_utc: str,
) -> tuple[str | None, float | None, str, bool, int]:
    policy = config["pnl"]["intrabar_ambiguity_policy"]
    times = sorted(t for t in prices if t >= entry_utc and t <= end_utc)
    bars = 0
    for t in times:
        bar = prices[t]
        bars += 1
        h, l = float(bar["h"]), float(bar["l"])
        hit_sl = h >= sl_price
        hit_tp = l <= tp_price
        if hit_sl and hit_tp:
            if policy == "no_trade_ambiguous":
                return t, None, "AMBIGUOUS_INTRABAR", True, bars
            return t, sl_price, "SL_CONSERVATIVE_UNRESOLVED", True, bars
        if hit_sl:
            return t, sl_price, "SL", False, bars
        if hit_tp:
            return t, tp_price, "TP", False, bars
    return None, None, "OPEN_AT_END", False, bars


def pool_still_active(
    pool_lo: float,
    pool_hi: float,
    snapshot: dict[str, Any],
    tol: float = 1e-9,
) -> bool:
    for b in snapshot.get("upper_bands") or []:
        lo, hi = float(b["lower_price"]), float(b["upper_price"])
        if abs(lo - pool_lo) < tol and abs(hi - pool_hi) < tol:
            return True
    return False

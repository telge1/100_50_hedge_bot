"""Frozen long entry geometry V1 (no up_4h gate)."""

from __future__ import annotations

from datetime import datetime

from . import config as C


def attached_lower(pool: dict, bar: dict) -> bool:
    if bar["close"] <= pool["top"]:
        return False
    if pool["top"] <= 0:
        return False
    if min(bar["open"], bar["close"]) <= pool["top"]:
        return False
    if bar["low"] - 1e-9 > pool["top"]:
        return False
    distance = (bar["close"] - pool["top"]) / pool["top"] * 100.0
    return distance <= C.ATTACH_PCT


def open_pools(
    pools: list[dict],
    side: str,
    moment: datetime,
    *,
    max_top: float | None = None,
) -> list[dict]:
    rows = [
        pool
        for pool in pools
        if pool["side"] == side
        and pool["known"] <= moment
        and (pool["break_at"] is None or pool["break_at"] > moment)
    ]
    if max_top is not None and side == "lower":
        rows = [pool for pool in rows if pool["top"] < max_top]
    return rows


def next_lower_bridge(pools: list[dict], current: dict, moment: datetime) -> dict | None:
    choices = [
        pool
        for pool in pools
        if pool["side"] == "lower"
        and pool["pool_id"] != current["pool_id"]
        and pool["known"] <= moment
        and pool["top"] < current["bottom"]
        and (pool["break_at"] is None or pool["break_at"] > moment)
    ]
    if not choices:
        return None
    choices.sort(key=lambda pool: (-pool["top"], pool["known"]))
    for pool in choices:
        bridge_gap = (current["bottom"] - pool["top"]) / current["bottom"] * 100.0
        if bridge_gap >= C.MIN_BRIDGE_GAP_PCT:
            return pool
    return None


def next_upper_tp(pools: list[dict], close: float, moment: datetime, min_gap_pct: float) -> dict | None:
    choices = [
        pool
        for pool in pools
        if pool["side"] == "upper"
        and pool["known"] <= moment
        and pool["bottom"] > close
        and (pool["break_at"] is None or pool["break_at"] > moment)
    ]
    if not choices:
        return None
    choices.sort(key=lambda pool: (pool["bottom"], pool["known"]))
    for pool in choices:
        gap_pct = (pool["bottom"] - close) / close * 100.0
        if gap_pct >= min_gap_pct:
            return pool
    return None


def gap_pct_bridge(current: dict, nxt: dict | None) -> float | None:
    if nxt is None or current["bottom"] <= 0:
        return None
    return (current["bottom"] - nxt["top"]) / current["bottom"] * 100.0


def entry_setup_long(pools: list[dict], bar: dict, moment: datetime) -> tuple[dict, dict, dict, float] | None:
    touched = [
        pool
        for pool in open_pools(pools, "lower", moment, max_top=bar["close"])
        if bar["close"] < bar["open"] and attached_lower(pool, bar)
    ]
    if not touched:
        return None
    pool = max(touched, key=lambda item: (item["top"], item["known"]))
    bridge_lower = next_lower_bridge(pools, pool, moment)
    gap = gap_pct_bridge(pool, bridge_lower)
    if bridge_lower is None or gap is None or gap < C.MIN_POOL_BRIDGE_GAP_PCT:
        return None
    upper = next_upper_tp(pools, bar["close"], moment, C.MIN_TP_PCT)
    if upper is None:
        return None
    return pool, bridge_lower, upper, gap


def row_for_signal(
    symbol: str,
    pool: dict,
    birth: dict,
    entry: dict,
    bridge_lower: dict,
    upper: dict,
    gap: float,
) -> dict:
    entry_price = float(entry["close"])
    stop = round(pool["bottom"] * (1.0 - C.STOP_PAD), 8)
    tp = float(upper["bottom"])
    tp_gap = round((tp - entry_price) / entry_price * 100.0, 4) if entry_price > 0 else None
    return {
        "symbol": symbol,
        "entry_time": entry["open_time"].isoformat(),
        "pool_id": pool["pool_id"],
        "entry_price": entry_price,
        "stop": stop,
        "tp": tp,
        "gap_pct": round(gap, 4),
        "tp_gap_pct": tp_gap,
        "known_at": pool["known"].isoformat(),
        "tp_pool_id": upper["pool_id"],
    }

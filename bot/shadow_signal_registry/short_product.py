"""Consolidate short baseline rows to one product candidate per pool."""

from __future__ import annotations

from typing import Any

from bot.shadow_signal_registry.models import BLOCK_REASON_NONE


def consolidate_short_product(enriched_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """One registry candidate per pool on this bar. BASELINE_NO_GUARD excluded."""
    if not enriched_rows:
        return None
    by_pool: dict[str, list[dict]] = {}
    for r in enriched_rows:
        pid = r.get("pool_id")
        if not pid:
            continue
        by_pool.setdefault(pid, []).append(r)

    if len(by_pool) != 1:
        pools = list(by_pool.keys())
        pid = pools[0]
    else:
        pid = next(iter(by_pool.keys()))
    rows = by_pool[pid]

    floor = [r for r in rows if r.get("variant") == "floor_blocked"]
    if floor:
        r = floor[0]
        return {
            "symbol": r["symbol"],
            "decision_time": r["decision_time"],
            "entry_price": r["entry_price"],
            "stop": r["stop"],
            "tp": r["tp"],
            "pool_id": r["pool_id"],
            "allowed": False,
            "blocked": True,
            "hypothetical": True,
            "block_reason": "FLOOR_GUARD",
            "raw_block_reason": r.get("block_reason") or r.get("floor_reason"),
            "signal_status": "BLOCKED",
            "e1r_state": r.get("e1r_state"),
            "e1r_block_reason": None,
            "floor_guard_state": "BLOCKED",
        }

    wg = [r for r in rows if r.get("variant") == "with_guard"]
    if not wg:
        return None
    r = wg[0]
    if r.get("signal_status") == "ALLOWED":
        return {
            "symbol": r["symbol"],
            "decision_time": r["decision_time"],
            "entry_price": r["entry_price"],
            "stop": r["stop"],
            "tp": r["tp"],
            "pool_id": r["pool_id"],
            "allowed": True,
            "blocked": False,
            "hypothetical": False,
            "block_reason": BLOCK_REASON_NONE,
            "raw_block_reason": None,
            "signal_status": "ALLOWED",
            "e1r_state": r.get("e1r_state"),
            "e1r_block_reason": None,
            "floor_guard_state": "PASS",
        }
    return {
        "symbol": r["symbol"],
        "decision_time": r["decision_time"],
        "entry_price": r["entry_price"],
        "stop": r["stop"],
        "tp": r["tp"],
        "pool_id": r["pool_id"],
        "allowed": False,
        "blocked": True,
        "hypothetical": True,
        "block_reason": "E1R_ACTIVE" if r.get("e1r_state") == "ACTIVE" else (r.get("block_reason") or "E1R"),
        "raw_block_reason": r.get("block_reason"),
        "signal_status": "BLOCKED",
        "e1r_state": r.get("e1r_state"),
        "e1r_block_reason": r.get("block_reason"),
        "floor_guard_state": "PASS",
    }

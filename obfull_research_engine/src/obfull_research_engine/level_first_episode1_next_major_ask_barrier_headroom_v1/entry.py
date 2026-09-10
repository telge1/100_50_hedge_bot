"""Executable long entry from BBO — never mid as fillable entry."""

from __future__ import annotations

from typing import Any

from . import ESTIMATED_ENTRY_SLIPPAGE_PCT, EPSILON, TICK_SIZE


def executable_long_entry(
    *,
    best_bid: float | None,
    best_ask: float | None,
    microprice: float | None = None,
    last_event_time: str | None = None,
    decision_time: str,
    replay_epoch: int | None,
    entry_slippage_pct: float = ESTIMATED_ENTRY_SLIPPAGE_PCT,
) -> dict[str, Any]:
    if best_bid is None or best_ask is None or best_ask <= 0 or best_bid <= 0:
        return {
            "ok": False,
            "reason": "BBO_INVALID",
            "best_bid_at_decision": best_bid,
            "best_ask_at_decision": best_ask,
            "decision_time": decision_time,
        }
    mid = 0.5 * (float(best_bid) + float(best_ask))
    spread = float(best_ask) - float(best_bid)
    spread_bps = (spread / mid) * 10_000.0 if mid > EPSILON else None
    theo = float(best_ask)
    entry_slip = float(entry_slippage_pct)
    with_slip = theo * (1.0 + entry_slip / 100.0)
    return {
        "ok": True,
        "decision_time": decision_time,
        "best_bid_at_decision": float(best_bid),
        "best_ask_at_decision": float(best_ask),
        "midprice_at_decision": mid,
        "microprice_at_decision": float(microprice) if microprice is not None else None,
        "theoretical_long_entry": theo,
        "long_entry_with_slippage": with_slip,
        "spread": spread,
        "spread_bps": spread_bps,
        "spread_ticks": spread / TICK_SIZE,
        "book_source_age": last_event_time,
        "replay_epoch": replay_epoch,
        "note": "Long entry uses best_ask, never midprice.",
    }

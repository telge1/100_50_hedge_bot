"""Filter frozen reference candidates/trades from checkpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import constants as C
from .causality import as_utc


def is_excluded_symbol(symbol: str) -> bool:
    return str(symbol).upper() in C.EXCLUDE_SYMBOLS


def is_reference_trade(trade: dict[str, Any]) -> bool:
    return (
        str(trade.get("timeframe")) == C.REF_TIMEFRAME
        and str(trade.get("mode_id")) == C.REF_MODE
        and str(trade.get("group")) == C.REF_GROUP
        and str(trade.get("strategy_key")) == C.REF_STRATEGY_KEY
    )


def entry_rule_ok(decision_at: Any, entry_at: Any) -> bool:
    if decision_at is None or entry_at is None:
        return False
    return as_utc(entry_at) >= as_utc(decision_at)


def filter_reference_trades(trades: list[dict[str, Any]], *, symbol: str | None = None) -> list[dict[str, Any]]:
    out = []
    for t in trades:
        sym = str(t.get("symbol") or symbol or "").upper()
        if is_excluded_symbol(sym):
            continue
        if not is_reference_trade(t):
            continue
        out.append(t)
    return out


def join_candidates_trades(
    candidates: list[dict[str, Any]],
    trades: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """One reference trade per candidate_id; drop XRP and non-reference."""
    cand_by_id = {str(c["candidate_id"]): c for c in candidates if not is_excluded_symbol(str(c.get("symbol", "")))}
    ref = filter_reference_trades(trades)
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen: set[str] = set()
    for t in ref:
        cid = str(t.get("candidate_id"))
        if cid in seen:
            continue
        c = cand_by_id.get(cid)
        if c is None:
            continue
        # Ensure candidate is SUPPORTIVE M0 5m
        if str(c.get("timeframe")) != C.REF_TIMEFRAME or str(c.get("mode_id")) != C.REF_MODE:
            continue
        if str(c.get("core_research_verdict")) != C.REF_GROUP:
            continue
        pairs.append((c, t))
        seen.add(cid)
    return pairs

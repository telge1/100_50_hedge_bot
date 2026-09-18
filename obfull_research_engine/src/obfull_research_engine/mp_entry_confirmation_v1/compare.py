"""Original-entry vs confirmed-entry classification."""

from __future__ import annotations

from typing import Any


def classify_original_vs_confirmed(
    *,
    original_result: str | None,
    confirmed_result: str | None,
    confirmed_entry: bool,
) -> str:
    """Map pair of target/stop results into comparison class."""
    o = (original_result or "").upper()
    c = (confirmed_result or "").upper() if confirmed_entry else None

    def is_win(r: str | None) -> bool:
        return r == "TARGET_FIRST"

    def is_loss(r: str | None) -> bool:
        return r in ("STOP_FIRST", "NEITHER", "CENSORED") or r == "AMBIGUOUS"

    if not confirmed_entry:
        if is_win(o):
            return "MISSED_WINNER"
        if is_loss(o) or o in ("STOP_FIRST", "NEITHER", "AMBIGUOUS", "CENSORED"):
            return "AVOIDED_BAD_TRADE"
        return "AMBIGUOUS"

    if is_win(o) and is_win(c):
        return "PRESERVED"
    if (not is_win(o)) and is_win(c):
        return "IMPROVED"
    if is_win(o) and (not is_win(c)):
        return "DEGRADED"
    if (not is_win(o)) and (not is_win(c)):
        return "UNCHANGED_FAILURE"
    return "AMBIGUOUS"


def entry_improved_vs_original(
    *,
    trade_side: str,
    original_price: float,
    confirmed_price: float,
) -> bool:
    side = trade_side.upper()
    if side == "LONG":
        return confirmed_price < original_price - 1e-15
    if side == "SHORT":
        return confirmed_price > original_price + 1e-15
    return False


def price_diff_pct(*, trade_side: str, original_price: float, confirmed_price: float) -> float:
    """Signed: positive means worse fill vs original for the trade side."""
    side = trade_side.upper()
    if side == "LONG":
        return 100.0 * (confirmed_price - original_price) / original_price
    return 100.0 * (original_price - confirmed_price) / original_price


def summarize_comparisons(rows: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        k = str(r.get("comparison_class") or "AMBIGUOUS")
        out[k] = out.get(k, 0) + 1
    return out

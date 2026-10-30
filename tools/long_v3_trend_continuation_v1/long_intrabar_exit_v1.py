"""Long intrabar SL/TP resolution (mirror BT-D82 short semantics)."""

from __future__ import annotations


def long_intrabar_exit_decision(
    *,
    ohlc: dict[str, float],
    sl_price: float,
    tp_price: float,
    policy: str,
) -> tuple[str | None, float | None, bool]:
    h, l = float(ohlc["h"]), float(ohlc["l"])
    hit_sl = l <= sl_price
    hit_tp = h >= tp_price
    if hit_sl and hit_tp:
        if policy == "no_trade_ambiguous":
            return "AMBIGUOUS_INTRABAR", None, True
        return "SL", sl_price, True
    if hit_sl:
        return "SL", sl_price, False
    if hit_tp:
        return "TP", tp_price, False
    return None, None, False


def gross_long_return_pct(entry: float, exit_price: float) -> float:
    return (exit_price - entry) / entry * 100.0

"""Minimal helpers for V2 (avoid pulling unrelated discovery packages)."""

from __future__ import annotations

TICK_BY_SYMBOL = {
    "BTCUSDT": 0.1,
    "DOGEUSDT": 0.00001,
    "XRPUSDT": 0.0001,
}


def tick_size(symbol: str) -> float:
    return float(TICK_BY_SYMBOL.get(str(symbol).upper(), 0.01))


def notional(price: float, qty: float) -> float:
    return float(price) * float(qty)

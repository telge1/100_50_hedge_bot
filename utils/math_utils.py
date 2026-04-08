from __future__ import annotations

from typing import Iterable


def safe_divide(numerator: float, denominator: float, default: float = 0.0) -> float:
    return numerator / denominator if denominator else default


def weighted_average(numbers: Iterable[float], weights: Iterable[float]) -> float:
    total_weight = sum(weights)
    if not total_weight:
        return 0.0
    return sum(value * weight for value, weight in zip(numbers, weights)) / total_weight


def calculate_pnl(entry_price: float, exit_price: float, size: float, side: str) -> float:
    direction = 1 if side.lower() == "long" else -1
    return direction * (exit_price - entry_price) * size


def adjust_qty_to_min_notional(
    qty: float, price: float, min_notional: float, qty_step: float
) -> float:
    notional = qty * price

    if notional >= min_notional:
        return qty

    required_qty = min_notional / price

    # round to nearest valid step
    steps = round(required_qty / qty_step)
    adjusted_qty = steps * qty_step

    return adjusted_qty

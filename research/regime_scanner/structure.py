"""PA-specific swing structure classification (not exhaustion retest caps).

Exhaustion ``lower_high_momentum_weakness`` (0.75% distance cap) is a separate
concept. Here a clearly lower high remains a valid ``lower_high``.
"""

from __future__ import annotations

from typing import Any, Literal

SwingSide = Literal["high", "low"]
HighStructure = Literal["higher_high", "lower_high", "equal_high"]
LowStructure = Literal["higher_low", "lower_low", "equal_low"]
StructureLabel = HighStructure | LowStructure


def price_distance_pct(first_price: float, second_price: float) -> float | None:
    first = float(first_price)
    second = float(second_price)
    if first == 0.0:
        return None
    return abs(second - first) / abs(first) * 100.0


def absolute_epsilon(price: float, epsilon_pct: float) -> float:
    return abs(float(price)) * float(epsilon_pct) / 100.0


def classify_swing_structure(
    first: dict[str, Any] | float,
    second: dict[str, Any] | float,
    *,
    side: SwingSide,
    epsilon_pct: float = 0.01,
) -> dict[str, Any]:
    """Classify two same-side swings for Price Action (fully symmetric).

    High side
    ---------
    * equal_high   — |Δ|% ≤ epsilon_pct
    * higher_high  — second > first + ε
    * lower_high   — second < first − ε   (no max-distance cap)

    Low side
    --------
    * equal_low    — |Δ|% ≤ epsilon_pct
    * higher_low   — second > first + ε
    * lower_low    — second < first − ε
    """
    first_price = float(first["price"] if isinstance(first, dict) else first)
    second_price = float(second["price"] if isinstance(second, dict) else second)
    dist = price_distance_pct(first_price, second_price)
    eps = absolute_epsilon(first_price, epsilon_pct)

    if dist is not None and dist <= float(epsilon_pct):
        label: StructureLabel = "equal_high" if side == "high" else "equal_low"
    elif second_price > first_price + eps:
        label = "higher_high" if side == "high" else "higher_low"
    elif second_price < first_price - eps:
        label = "lower_high" if side == "high" else "lower_low"
    else:
        # Within absolute epsilon band but above pct gate — treat as equal.
        label = "equal_high" if side == "high" else "equal_low"

    return {
        "side": side,
        "structure_type": label,
        "first_price": first_price,
        "second_price": second_price,
        "price_distance_pct": dist,
        "epsilon_pct": float(epsilon_pct),
        "absolute_epsilon": eps,
    }

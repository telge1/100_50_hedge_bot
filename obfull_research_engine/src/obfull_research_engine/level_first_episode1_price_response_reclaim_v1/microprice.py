"""Microprice and midprice from causal BBO (+ sizes).

Formula (size-weighted, industry-standard microprice)
----------------------------------------------------
    microprice = (best_ask * best_bid_size + best_bid * best_ask_size)
                 / (best_bid_size + best_ask_size)

Interpretation:
  - Larger bid size pulls microprice toward the ask (buy pressure at bid).
  - Larger ask size pulls microprice toward the bid (sell pressure at ask).
  - When sizes are equal, microprice equals midprice.

Side semantics (Episode-1 wall context)
---------------------------------------
Ask wall (attackers are buyers pushing up):
  - Attack direction: +1 (higher prices)
  - Defender side: price < wall_price
  - Attacker / crossed side: price >= wall_price

Bid wall (attackers are sellers pushing down):
  - Attack direction: -1
  - Defender side: price > wall_price
  - Attacker / crossed side: price <= wall_price

This module does NOT classify reclaim thresholds.
"""

from __future__ import annotations

from . import EPSILON


def midprice(best_bid: float, best_ask: float) -> float:
    return 0.5 * (float(best_bid) + float(best_ask))


def microprice(
    best_bid: float,
    best_bid_size: float,
    best_ask: float,
    best_ask_size: float,
) -> float | None:
    """Size-weighted microprice; None if sizes are non-positive / degenerate."""
    bb = float(best_bid)
    ba = float(best_ask)
    bs = float(best_bid_size)
    asz = float(best_ask_size)
    denom = bs + asz
    if denom <= EPSILON:
        return None
    if bs < 0 or asz < 0:
        return None
    return (ba * bs + bb * asz) / denom


def attack_direction(wall_side: str) -> int:
    side = str(wall_side).strip().lower()
    if side == "ask":
        return 1
    if side == "bid":
        return -1
    raise ValueError(f"unsupported wall_side: {wall_side!r}")


def on_defender_side(price: float | None, *, wall_price: float, wall_side: str) -> bool | None:
    if price is None:
        return None
    d = attack_direction(wall_side)
    # Defender = opposite of attack.
    if d > 0:
        return float(price) < float(wall_price)
    return float(price) > float(wall_price)


def side_crossed(price: float | None, *, wall_price: float, wall_side: str) -> bool | None:
    """True when price is at/beyond the wall in the attack direction."""
    if price is None:
        return None
    d = attack_direction(wall_side)
    if d > 0:
        return float(price) >= float(wall_price)
    return float(price) <= float(wall_price)


def signed_distance_ticks(
    price: float | None,
    *,
    reference: float,
    tick_size: float,
    wall_side: str,
) -> float | None:
    """Positive = attack direction from reference toward/through wall."""
    if price is None:
        return None
    d = attack_direction(wall_side)
    return d * (float(price) - float(reference)) / float(tick_size)


MICROPRICE_FORMULA_DOC = (
    "microprice = (best_ask * best_bid_size + best_bid * best_ask_size) "
    "/ (best_bid_size + best_ask_size); "
    "ask-wall defender = price < wall; bid-wall defender = price > wall"
)

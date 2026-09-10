"""Side-normalized attack coordinates (Ask/Bid mirror)."""

from __future__ import annotations

from . import TICK_SIZE


def attack_direction(wall_side: str) -> int:
    side = str(wall_side).strip().lower()
    if side == "ask":
        return 1  # rising price = attack
    if side == "bid":
        return -1  # falling price = attack
    raise ValueError(f"unsupported wall_side: {wall_side!r}")


def on_attack_side(price: float | None, *, wall_price: float, wall_side: str) -> bool | None:
    if price is None:
        return None
    d = attack_direction(wall_side)
    if d > 0:
        return float(price) >= float(wall_price)
    return float(price) <= float(wall_price)


def on_defender_side(price: float | None, *, wall_price: float, wall_side: str) -> bool | None:
    if price is None:
        return None
    d = attack_direction(wall_side)
    if d > 0:
        return float(price) < float(wall_price)
    return float(price) > float(wall_price)


def side_label(price: float | None, *, wall_price: float, wall_side: str) -> str | None:
    if price is None:
        return None
    if on_attack_side(price, wall_price=wall_price, wall_side=wall_side):
        return "ATTACK"
    if on_defender_side(price, wall_price=wall_price, wall_side=wall_side):
        return "DEFENDER"
    return "UNKNOWN"


def attack_progress_ticks(
    price: float | None,
    *,
    reference: float,
    wall_side: str,
    tick_size: float = TICK_SIZE,
) -> float | None:
    """Positive = movement in attack direction from reference."""
    if price is None:
        return None
    d = attack_direction(wall_side)
    return d * (float(price) - float(reference)) / float(tick_size)


def defender_progress_ticks(
    price: float | None,
    *,
    reference: float,
    wall_side: str,
    tick_size: float = TICK_SIZE,
) -> float | None:
    """Positive = movement in defender direction from reference."""
    ap = attack_progress_ticks(price, reference=reference, wall_side=wall_side, tick_size=tick_size)
    return None if ap is None else -ap


def attack_progress_bps(price: float | None, *, reference: float, wall_side: str) -> float | None:
    if price is None or reference is None or float(reference) == 0:
        return None
    d = attack_direction(wall_side)
    return d * (float(price) - float(reference)) / float(reference) * 10000.0


def defender_progress_bps(price: float | None, *, reference: float, wall_side: str) -> float | None:
    ap = attack_progress_bps(price, reference=reference, wall_side=wall_side)
    return None if ap is None else -ap


SIDE_SEMANTICS_DOC = {
    "ask_wall": {
        "attack_direction": "+1 (higher price)",
        "attack_side": "price >= wall",
        "defender_side": "price < wall",
    },
    "bid_wall": {
        "attack_direction": "-1 (lower price)",
        "attack_side": "price <= wall",
        "defender_side": "price > wall",
    },
}

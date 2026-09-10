"""Past-only wall selection at zone_touch from reconstructed book (no future data)."""

from __future__ import annotations

from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.episodes import parse_utc
from ..level_first_episode1_touch_detection_independent_v1.wall_generations import (
    CoverageError,
    reconstruct_or_coverage_error,
)


def _levels(book_side: dict[Any, Any] | None) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for px, qty in (book_side or {}).items():
        q = float(qty)
        if q <= 0:
            continue
        out.append((float(px), q))
    return out


def select_ask_wall_above(
    asks: dict[Any, Any] | None,
    *,
    zone_high: float,
    tick_size: float = 0.1,
    distance_cap_ticks: int = 50,
) -> dict[str, Any] | None:
    """Nearest / largest ask wall above zone_high within distance cap (past-only)."""
    cap = float(distance_cap_ticks) * float(tick_size)
    candidates = []
    for px, qty in _levels(asks):
        if px <= float(zone_high):
            continue
        dist = px - float(zone_high)
        if dist > cap:
            continue
        notional = qty * px
        candidates.append((px, qty, dist, notional))
    if not candidates:
        return None
    # Prefer top size within cap; tie-break nearer to zone
    candidates.sort(key=lambda x: (-x[3], x[2], x[0]))
    px, qty, dist, notional = candidates[0]
    return {
        "wall_side": "ask",
        "wall_price": px,
        "qty": qty,
        "notional": notional,
        "distance_ticks": dist / float(tick_size),
        "selection_rule": "top_notional_within_distance_cap_above_zone_high",
    }


def select_bid_wall_below(
    bids: dict[Any, Any] | None,
    *,
    zone_low: float,
    tick_size: float = 0.1,
    distance_cap_ticks: int = 50,
) -> dict[str, Any] | None:
    """Nearest / largest bid wall below zone_low within distance cap (past-only)."""
    cap = float(distance_cap_ticks) * float(tick_size)
    candidates = []
    for px, qty in _levels(bids):
        if px >= float(zone_low):
            continue
        dist = float(zone_low) - px
        if dist > cap:
            continue
        notional = qty * px
        candidates.append((px, qty, dist, notional))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (-x[3], x[2], -x[0]))
    px, qty, dist, notional = candidates[0]
    return {
        "wall_side": "bid",
        "wall_price": px,
        "qty": qty,
        "notional": notional,
        "distance_ticks": dist / float(tick_size),
        "selection_rule": "top_notional_within_distance_cap_below_zone_low",
    }


def select_walls_at_zone_touch(
    replay: dict[str, Any],
    *,
    zone_touch_exchange_time: str | Any,
    zone_low: float,
    zone_high: float,
    tick_size: float = 0.1,
    distance_cap_ticks: int = 50,
) -> dict[str, Any]:
    """
    Reconstruct book at zone_touch (past-only) and select ask+bid wall candidates.
    """
    until = parse_utc(zone_touch_exchange_time)
    try:
        book = reconstruct_or_coverage_error(replay, until)
    except CoverageError as exc:
        return {
            "ok": False,
            "exclusion": "BOOK_REPLAY_FAILED",
            "detail": str(exc),
            "ask_wall": None,
            "bid_wall": None,
            "book": None,
        }
    ask = select_ask_wall_above(
        book.get("asks"),
        zone_high=zone_high,
        tick_size=tick_size,
        distance_cap_ticks=distance_cap_ticks,
    )
    bid = select_bid_wall_below(
        book.get("bids"),
        zone_low=zone_low,
        tick_size=tick_size,
        distance_cap_ticks=distance_cap_ticks,
    )
    return {
        "ok": True,
        "ask_wall": ask,
        "bid_wall": bid,
        "book": {
            "best_bid": book.get("best_bid"),
            "best_ask": book.get("best_ask"),
            "replay_epoch": book.get("replay_epoch"),
            "update_id": book.get("update_id"),
            "sequence_id": book.get("sequence_id"),
            "checkpoint_id": book.get("checkpoint_id"),
        },
        "has_any_wall": ask is not None or bid is not None,
    }

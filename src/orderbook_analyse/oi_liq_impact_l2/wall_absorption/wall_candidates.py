"""Per-level wall candidate extraction from causal orderbook snapshots."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence

from orderbook_analyse.oi_liq_impact_l2.wall_absorption.constants import WALL_TOP_N
from orderbook_analyse.orderbook_replay import OrderBookState

ZERO = Decimal("0")


@dataclass(frozen=True)
class WallCandidate:
    cluster_id: str
    symbol: str
    direction: str
    candidate_rank_type: str
    candidate_rank: int
    wall_price: Decimal
    wall_qty: Decimal
    distance_to_mid_ticks: Decimal | None
    distance_to_mid_bps: Decimal | None
    side_depth_share: Decimal | None
    qty_vs_side_median: Decimal | None
    qty_vs_neighbor_ratio: Decimal | None
    is_primary_anchor: bool
    sort_key: str


def _tick_size(price: Decimal) -> Decimal:
    text = format(price, "f")
    if "." in text:
        decimals = len(text.split(".")[1].rstrip("0"))
        return Decimal(10) ** Decimal(-max(decimals, 1))
    return Decimal("1")


def _relevant_levels(book: OrderBookState, direction: str) -> list[tuple[Decimal, Decimal]]:
    if direction == "LONG":
        best = book.best_bid()
        if best is None:
            return []
        return sorted(
            ((price, qty) for price, qty in book.bids.items() if price <= best),
            key=lambda item: (-item[0], -item[1], item[0]),
        )
    best = book.best_ask()
    if best is None:
        return []
    return sorted(
        ((price, qty) for price, qty in book.asks.items() if price >= best),
        key=lambda item: (item[0], -item[1], item[0]),
    )


def _distance_bps(price: Decimal, mid: Decimal | None) -> Decimal | None:
    if mid is None or mid == ZERO:
        return None
    return abs(price - mid) / mid * Decimal("10000")


def _side_depth_share(qty: Decimal, levels: Sequence[tuple[Decimal, Decimal]]) -> Decimal | None:
    total = sum((level_qty for _, level_qty in levels), ZERO)
    if total == ZERO:
        return None
    return qty / total


def _qty_vs_median(qty: Decimal, levels: Sequence[tuple[Decimal, Decimal]]) -> Decimal | None:
    values = [level_qty for _, level_qty in levels if level_qty > ZERO]
    if not values:
        return None
    median = Decimal(str(statistics.median([float(v) for v in values])))
    if median == ZERO:
        return None
    return qty / median


def _neighbor_ratio(
    price: Decimal,
    qty: Decimal,
    levels: Sequence[tuple[Decimal, Decimal]],
    *,
    direction: str,
) -> Decimal | None:
    prices = [p for p, _ in levels]
    if price not in prices:
        return None
    idx = prices.index(price)
    neighbors: list[Decimal] = []
    if idx > 0:
        neighbors.append(levels[idx - 1][1])
    if idx + 1 < len(levels):
        neighbors.append(levels[idx + 1][1])
    if not neighbors:
        return None
    neighbor_avg = sum(neighbors, ZERO) / Decimal(len(neighbors))
    if neighbor_avg == ZERO:
        return None
    return qty / neighbor_avg


def build_wall_candidates(
    *,
    cluster_id: str,
    symbol: str,
    direction: str,
    book: OrderBookState,
) -> list[WallCandidate]:
    levels = _relevant_levels(book, direction)
    if not levels:
        return []
    mid = book.mid_price()
    tick = _tick_size(levels[0][0])

    by_qty = sorted(
        levels,
        key=lambda item: (-item[1], abs(item[0] - (mid or item[0])), item[0]),
    )[:WALL_TOP_N]
    by_distance = sorted(
        levels,
        key=lambda item: (
            abs(item[0] - (mid or item[0])),
            -item[1],
            item[0],
        ),
    )[:WALL_TOP_N]

    primary_price = by_qty[0][0]
    candidates: list[WallCandidate] = []

    def append_candidate(
        rank_type: str,
        rank: int,
        price: Decimal,
        qty: Decimal,
    ) -> None:
        distance = abs(price - mid) if mid is not None else None
        distance_ticks = (distance / tick) if distance is not None else None
        sort_key = f"{price}|{qty}|{rank_type}|{rank}"
        candidates.append(
            WallCandidate(
                cluster_id=cluster_id,
                symbol=symbol,
                direction=direction,
                candidate_rank_type=rank_type,
                candidate_rank=rank,
                wall_price=price,
                wall_qty=qty,
                distance_to_mid_ticks=distance_ticks,
                distance_to_mid_bps=_distance_bps(price, mid),
                side_depth_share=_side_depth_share(qty, levels),
                qty_vs_side_median=_qty_vs_median(qty, levels),
                qty_vs_neighbor_ratio=_neighbor_ratio(
                    price, qty, levels, direction=direction
                ),
                is_primary_anchor=price == primary_price,
                sort_key=sort_key,
            )
        )

    for idx, (price, qty) in enumerate(by_qty, start=1):
        append_candidate("BY_QTY", idx, price, qty)
    for idx, (price, qty) in enumerate(by_distance, start=1):
        append_candidate("BY_DISTANCE", idx, price, qty)

    deduped: dict[tuple[str, Decimal], WallCandidate] = {}
    for candidate in candidates:
        key = (candidate.candidate_rank_type, candidate.wall_price)
        deduped[key] = candidate
    ordered = sorted(deduped.values(), key=lambda item: item.sort_key)
    rebuilt: list[WallCandidate] = []
    for candidate in ordered:
        rebuilt.append(
            WallCandidate(
                cluster_id=candidate.cluster_id,
                symbol=candidate.symbol,
                direction=candidate.direction,
                candidate_rank_type=candidate.candidate_rank_type,
                candidate_rank=candidate.candidate_rank,
                wall_price=candidate.wall_price,
                wall_qty=candidate.wall_qty,
                distance_to_mid_ticks=candidate.distance_to_mid_ticks,
                distance_to_mid_bps=candidate.distance_to_mid_bps,
                side_depth_share=candidate.side_depth_share,
                qty_vs_side_median=candidate.qty_vs_side_median,
                qty_vs_neighbor_ratio=candidate.qty_vs_neighbor_ratio,
                is_primary_anchor=(
                    candidate.candidate_rank_type == "BY_QTY"
                    and candidate.candidate_rank == 1
                    and candidate.wall_price == primary_price
                ),
                sort_key=candidate.sort_key,
            )
        )
    return rebuilt


def wall_candidate_row(candidate: WallCandidate) -> dict[str, object]:
    return {
        "cluster_id": candidate.cluster_id,
        "symbol": candidate.symbol,
        "direction": candidate.direction,
        "candidate_rank_type": candidate.candidate_rank_type,
        "candidate_rank": candidate.candidate_rank,
        "wall_price": float(candidate.wall_price),
        "wall_qty": float(candidate.wall_qty),
        "distance_to_mid_ticks": float(candidate.distance_to_mid_ticks)
        if candidate.distance_to_mid_ticks is not None
        else None,
        "distance_to_mid_bps": float(candidate.distance_to_mid_bps)
        if candidate.distance_to_mid_bps is not None
        else None,
        "side_depth_share": float(candidate.side_depth_share)
        if candidate.side_depth_share is not None
        else None,
        "qty_vs_side_median": float(candidate.qty_vs_side_median)
        if candidate.qty_vs_side_median is not None
        else None,
        "qty_vs_neighbor_ratio": float(candidate.qty_vs_neighbor_ratio)
        if candidate.qty_vs_neighbor_ratio is not None
        else None,
        "is_primary_anchor": candidate.is_primary_anchor,
        "sort_key": candidate.sort_key,
    }

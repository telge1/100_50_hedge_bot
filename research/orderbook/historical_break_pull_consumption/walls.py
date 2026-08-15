"""Wall-zone tracking and trade matching (pull vs consumption)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from research.orderbook.historical_break_pull_consumption import (
    MATCH_PRICE_BPS,
    MATCH_TIME_MS,
    WALL_SELECT_BPS,
    ZONE_BPS,
)
from research.orderbook.historical_break_pull_consumption.trades import Trade
from research.orderbook.historical_bybit_replay import OrderBook


@dataclass
class WallSnapshot:
    ts_ms: int
    wall_price: float | None
    wall_qty: float
    zone_qty: float
    best_bid: float | None
    best_ask: float | None
    mid: float | None
    distance_to_level_bps: float | None


@dataclass
class WallAction:
    event_id: str
    ts_ms: int
    action: str  # ADD INCREASE DECREASE DELETE REAPPEAR RELOCATE
    wall_price: float | None
    qty_before: float
    qty_after: float
    delta_qty: float
    zone_qty_before: float
    zone_qty_after: float
    best_bid: float | None
    best_ask: float | None
    mid: float | None
    distance_to_level_bps: float | None
    matched_aggressive_qty: float = 0.0
    matched_trade_count: int = 0
    unmatched_removal_qty: float = 0.0
    consumption_ratio: float | None = None
    mechanism_hint: str = ""


def _f(x: Decimal | float | None) -> float | None:
    if x is None:
        return None
    return float(x)


def zone_qty(book: OrderBook, *, book_side: str, level: float, zone_bps: float = ZONE_BPS) -> float:
    if level <= 0:
        return 0.0
    band = level * zone_bps / 10_000.0
    levels = book.bids if book_side == "bid" else book.asks
    total = 0.0
    for px, qty in levels.items():
        p, q = float(px), float(qty)
        if q <= 0:
            continue
        if abs(p - level) <= band:
            total += q
    return total


def dominant_wall(
    book: OrderBook,
    *,
    book_side: str,
    level: float,
    max_bps: float = WALL_SELECT_BPS,
) -> tuple[float | None, float]:
    if level <= 0:
        return None, 0.0
    levels = book.bids if book_side == "bid" else book.asks
    best_px, best_q = None, 0.0
    for px, qty in levels.items():
        p, q = float(px), float(qty)
        if q <= 0:
            continue
        if abs(p - level) / level * 1e4 > max_bps:
            continue
        # Prefer closer walls when qty similar: score = qty / (1 + dist_bps)
        dist = abs(p - level) / level * 1e4
        score = q / (1.0 + dist / 5.0)
        best_score = best_q / (1.0 + (abs(best_px - level) / level * 1e4) / 5.0) if best_px is not None else -1.0
        if score > best_score:
            best_px, best_q = p, q
    return best_px, best_q


def snapshot_wall(
    book: OrderBook,
    *,
    ts_ms: int,
    level: float,
    book_side: str,
) -> WallSnapshot:
    bb = _f(book.best_bid())
    ba = _f(book.best_ask())
    mid = None if bb is None or ba is None else (bb + ba) / 2.0
    dist = None if mid is None or level <= 0 else (mid - level) / level * 1e4
    wpx, wq = dominant_wall(book, book_side=book_side, level=level)
    zq = zone_qty(book, book_side=book_side, level=level)
    return WallSnapshot(
        ts_ms=ts_ms,
        wall_price=wpx,
        wall_qty=wq,
        zone_qty=zq,
        best_bid=bb,
        best_ask=ba,
        mid=mid,
        distance_to_level_bps=dist,
    )


def classify_action(
    *,
    prev: WallSnapshot | None,
    cur: WallSnapshot,
) -> str | None:
    if prev is None:
        if cur.zone_qty > 0 or cur.wall_qty > 0:
            return "ADD"
        return None
    # Relocate: dominant price moved while qty still present
    if (
        prev.wall_price is not None
        and cur.wall_price is not None
        and abs(prev.wall_price - cur.wall_price) / max(abs(cur.wall_price), 1e-12) * 1e4 > 2.0
        and cur.wall_qty > 0
        and prev.wall_qty > 0
    ):
        return "RELOCATE"
    dz = cur.zone_qty - prev.zone_qty
    if prev.zone_qty > 0 and cur.zone_qty <= 0:
        return "DELETE"
    if prev.zone_qty <= 0 and cur.zone_qty > 0:
        return "REAPPEAR"
    if dz > max(1e-12, prev.zone_qty * 0.02):
        return "INCREASE"
    if dz < -max(1e-12, prev.zone_qty * 0.02):
        return "DECREASE"
    # also track wall_qty shrink without zone change threshold using wall_qty
    dw = cur.wall_qty - prev.wall_qty
    if dw < -max(1e-12, prev.wall_qty * 0.05) and prev.wall_qty > 0:
        return "DECREASE"
    if dw > max(1e-12, prev.wall_qty * 0.05):
        return "INCREASE"
    return None


def match_aggressive_trades(
    trades: list[Trade],
    *,
    action_ts_ms: int,
    aggressor_side: str,
    ref_price: float,
    match_time_ms: int = MATCH_TIME_MS,
    match_price_bps: float = MATCH_PRICE_BPS,
) -> tuple[float, int, list[Trade]]:
    """Match aggressor trades near action time/price. Causal: trades <= action_ts + tol,
    and trades >= action_ts - tol (feed sync window). Exclude trades after action+tol.
    """
    if ref_price <= 0:
        return 0.0, 0, []
    lo = action_ts_ms - match_time_ms
    hi = action_ts_ms + match_time_ms
    band = ref_price * match_price_bps / 10_000.0
    matched: list[Trade] = []
    qty = 0.0
    for t in trades:
        if t.ts_ms < lo or t.ts_ms > hi:
            continue
        if t.side != aggressor_side:
            continue
        if abs(t.price - ref_price) > band:
            continue
        matched.append(t)
        qty += t.size
    return qty, len(matched), matched


def build_actions_from_snaps(
    event_id: str,
    snaps: list[WallSnapshot],
    *,
    level: float,
    trades: list[Trade],
    aggressor_side: str,
) -> list[WallAction]:
    out: list[WallAction] = []
    prev: WallSnapshot | None = None
    for cur in snaps:
        act = classify_action(prev=prev, cur=cur)
        if act is None or prev is None:
            prev = cur
            continue
        qty_before = prev.zone_qty
        qty_after = cur.zone_qty
        delta = qty_after - qty_before
        ref = cur.wall_price or prev.wall_price or level
        matched_qty = 0.0
        matched_n = 0
        ratio = None
        hint = ""
        unmatched = 0.0
        if act in {"DECREASE", "DELETE"} and delta < 0:
            removed = -delta
            matched_qty, matched_n, _ = match_aggressive_trades(
                trades,
                action_ts_ms=cur.ts_ms,
                aggressor_side=aggressor_side,
                ref_price=ref,
            )
            ratio = matched_qty / removed if removed > 0 else None
            unmatched = max(0.0, removed - matched_qty)
            if ratio is None:
                hint = ""
            elif ratio < 0.30:
                hint = "PULLISH"
            elif ratio > 0.70:
                hint = "CONSUMPTIONISH"
            else:
                hint = "MIXEDISH"
        elif act in {"INCREASE", "REAPPEAR", "ADD"}:
            hint = "REFILLISH" if act in {"INCREASE", "REAPPEAR"} else "ADD"
        out.append(
            WallAction(
                event_id=event_id,
                ts_ms=cur.ts_ms,
                action=act,
                wall_price=cur.wall_price or prev.wall_price,
                qty_before=qty_before,
                qty_after=qty_after,
                delta_qty=delta,
                zone_qty_before=qty_before,
                zone_qty_after=qty_after,
                best_bid=cur.best_bid,
                best_ask=cur.best_ask,
                mid=cur.mid,
                distance_to_level_bps=cur.distance_to_level_bps,
                matched_aggressive_qty=matched_qty,
                matched_trade_count=matched_n,
                unmatched_removal_qty=unmatched,
                consumption_ratio=ratio,
                mechanism_hint=hint,
            )
        )
        prev = cur
    return out


def aggressive_flow_in_window(
    trades: list[Trade],
    *,
    start_ms: int,
    end_ms: int,
    aggressor_side: str,
    ref_price: float,
    match_price_bps: float = MATCH_PRICE_BPS * 3,
) -> float:
    if ref_price <= 0:
        return 0.0
    band = ref_price * match_price_bps / 10_000.0
    total = 0.0
    for t in trades:
        if t.ts_ms < start_ms or t.ts_ms > end_ms:
            continue
        if t.side != aggressor_side:
            continue
        if abs(t.price - ref_price) > band:
            continue
        total += t.size
    return total


def opposite_flow_in_window(
    trades: list[Trade],
    *,
    start_ms: int,
    end_ms: int,
    aggressor_side: str,
) -> float:
    opp = "Buy" if aggressor_side == "Sell" else "Sell"
    return sum(t.size for t in trades if start_ms <= t.ts_ms <= end_ms and t.side == opp)

"""Derive 1-second feature rows from order book state.

Imbalance formula (documented):
    (bid_depth - ask_depth) / (bid_depth + ask_depth)
    Returns 0.0 when both sides are 0 (safe division).

Wall: largest visible level by qty within 200 bps of mid.
Wall ratio: level qty / median level qty within that range (or 0 if 1 level).
"""
from __future__ import annotations

import statistics
from decimal import Decimal
from typing import Any

from orderbook_analyse.orderbook_v2.book import ZERO, BookState, sorted_asks, sorted_bids
from orderbook_analyse.orderbook_v2 import PARSER_VERSION
WALL_MAX_BPS = Decimal("200")


def _imbalance(bid_d: Decimal, ask_d: Decimal) -> Decimal:
    total = bid_d + ask_d
    if total == ZERO:
        return ZERO
    return (bid_d - ask_d) / total


def _depth_by_levels(
    levels: list[tuple[Decimal, Decimal]], n: int
) -> tuple[Decimal, Decimal]:
    """(qty, notional) for top N levels."""
    qty = ZERO
    notional = ZERO
    for p, q in levels[:n]:
        qty += q
        notional += p * q
    return qty, notional


def _depth_by_bps(
    levels: list[tuple[Decimal, Decimal]],
    mid: Decimal,
    bps: Decimal,
    is_bid: bool,
) -> tuple[Decimal, Decimal]:
    """qty, notional within bps distance from mid for bid or ask side."""
    qty = ZERO
    notional = ZERO
    if mid == ZERO:
        return qty, notional
    threshold = mid * bps / Decimal("10000")
    for p, q in levels:
        dist = abs(p - mid)
        if dist <= threshold:
            qty += q
            notional += p * q
        else:
            # bids are sorted desc: once dist > threshold and going away from mid, break
            # asks are sorted asc: same
            if is_bid and p < mid - threshold:
                break
            if not is_bid and p > mid + threshold:
                break
    return qty, notional


def _wall(
    levels: list[tuple[Decimal, Decimal]],
    mid: Decimal,
    max_bps: Decimal = WALL_MAX_BPS,
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
    """(price, qty, notional, bps_dist, ratio).
    ratio = qty / median(all level qtys in range); 0 if only 1 level or mid=0.
    """
    if not levels or mid == ZERO:
        return ZERO, ZERO, ZERO, ZERO, ZERO
    threshold = mid * max_bps / Decimal("10000")
    in_range = [(p, q) for p, q in levels if abs(p - mid) <= threshold]
    if not in_range:
        return ZERO, ZERO, ZERO, ZERO, ZERO
    wall_p, wall_q = max(in_range, key=lambda x: x[1])
    notional = wall_p * wall_q
    bps_dist = abs(wall_p - mid) / mid * Decimal("10000")
    qtys = [float(q) for _, q in in_range]
    ratio = Decimal(str(float(wall_q) / statistics.median(qtys))) if len(qtys) > 1 else ZERO
    return wall_p, wall_q, notional, bps_dist, ratio


def compute_features(
    book: BookState,
    bucket_start_ms: int,
    first_ts_ms: int,
    last_ts_ms: int,
    processed_updates: int,
    *,
    exchange: str = "bybit",
    market: str = "linear",
    symbol: str,
    depth: int = 200,
    quality_flags: list[str] | None = None,
    # dynamics (nullable)
    bid_qty_added: Decimal | None = None,
    bid_qty_removed: Decimal | None = None,
    ask_qty_added: Decimal | None = None,
    ask_qty_removed: Decimal | None = None,
    bid_add_count: int | None = None,
    bid_remove_count: int | None = None,
    ask_add_count: int | None = None,
    ask_remove_count: int | None = None,
    ofi: Decimal | None = None,
    mid_price_change: Decimal | None = None,
    imbalance_l10_change: Decimal | None = None,
    imbalance_l50_change: Decimal | None = None,
) -> dict[str, Any]:
    from datetime import datetime, timezone

    def ms_to_dt(ms: int) -> datetime:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)

    bids = sorted_bids(book)
    asks = sorted_asks(book)

    if not bids or not asks or not book.is_valid:
        return _invalid_row(
            exchange=exchange, market=market, symbol=symbol, depth=depth,
            bucket_start_ms=bucket_start_ms, first_ts_ms=first_ts_ms,
            last_ts_ms=last_ts_ms, last_u=book.last_u,
            processed_updates=processed_updates,
            quality_flags=quality_flags or ["empty_book"],
        )

    best_bid_price, best_bid_qty = bids[0]
    best_ask_price, best_ask_qty = asks[0]

    if best_bid_price >= best_ask_price:
        return _invalid_row(
            exchange=exchange, market=market, symbol=symbol, depth=depth,
            bucket_start_ms=bucket_start_ms, first_ts_ms=first_ts_ms,
            last_ts_ms=last_ts_ms, last_u=book.last_u,
            processed_updates=processed_updates,
            quality_flags=(quality_flags or []) + ["crossed_book"],
        )

    mid = (best_bid_price + best_ask_price) / Decimal("2")

    # microprice
    total_top_qty = best_bid_qty + best_ask_qty
    if total_top_qty > ZERO:
        microprice = (best_ask_price * best_bid_qty + best_bid_price * best_ask_qty) / total_top_qty
    else:
        microprice = mid

    spread_abs = best_ask_price - best_bid_price
    spread_bps = spread_abs / mid * Decimal("10000") if mid > ZERO else ZERO

    def dl(n: int) -> dict[str, Decimal]:
        bq, bn = _depth_by_levels(bids, n)
        aq, an = _depth_by_levels(asks, n)
        return {"bq": bq, "bn": bn, "aq": aq, "an": an, "imb": _imbalance(bq, aq)}

    l5 = dl(5); l10 = dl(10); l25 = dl(25); l50 = dl(50)

    def db(bps_val: int) -> dict[str, Decimal]:
        bps = Decimal(str(bps_val))
        bq, bn = _depth_by_bps(bids, mid, bps, is_bid=True)
        aq, an = _depth_by_bps(asks, mid, bps, is_bid=False)
        return {"bq": bq, "bn": bn, "aq": aq, "an": an, "imb": _imbalance(bq, aq)}

    d5 = db(5); d10 = db(10); d25 = db(25); d50 = db(50)

    bwp, bwq, bwn, bwd, bwr = _wall(bids, mid)
    awp, awq, awn, awd, awr = _wall(asks, mid)

    qf = ",".join(quality_flags) if quality_flags else ""

    return {
        "exchange": exchange, "market": market, "symbol": symbol, "depth": depth,
        "bucket_start": ms_to_dt(bucket_start_ms),
        "first_source_ts": ms_to_dt(first_ts_ms),
        "last_source_ts": ms_to_dt(last_ts_ms),
        "last_update_seq": book.last_u,
        "processed_updates": processed_updates,
        "parser_version": PARSER_VERSION,
        "created_at": datetime.now(timezone.utc),
        "quality_flags": qf,
        "is_valid": 1,
        "best_bid_price": best_bid_price, "best_bid_qty": best_bid_qty,
        "best_ask_price": best_ask_price, "best_ask_qty": best_ask_qty,
        "mid_price": mid, "microprice": microprice,
        "spread_abs": spread_abs, "spread_bps": spread_bps,
        "bid_qty_l5": l5["bq"], "ask_qty_l5": l5["aq"],
        "bid_notional_l5": l5["bn"], "ask_notional_l5": l5["an"], "imbalance_l5": l5["imb"],
        "bid_qty_l10": l10["bq"], "ask_qty_l10": l10["aq"],
        "bid_notional_l10": l10["bn"], "ask_notional_l10": l10["an"], "imbalance_l10": l10["imb"],
        "bid_qty_l25": l25["bq"], "ask_qty_l25": l25["aq"],
        "bid_notional_l25": l25["bn"], "ask_notional_l25": l25["an"], "imbalance_l25": l25["imb"],
        "bid_qty_l50": l50["bq"], "ask_qty_l50": l50["aq"],
        "bid_notional_l50": l50["bn"], "ask_notional_l50": l50["an"], "imbalance_l50": l50["imb"],
        "bid_qty_bps5": d5["bq"], "ask_qty_bps5": d5["aq"],
        "bid_notional_bps5": d5["bn"], "ask_notional_bps5": d5["an"], "imbalance_bps5": d5["imb"],
        "bid_qty_bps10": d10["bq"], "ask_qty_bps10": d10["aq"],
        "bid_notional_bps10": d10["bn"], "ask_notional_bps10": d10["an"], "imbalance_bps10": d10["imb"],
        "bid_qty_bps25": d25["bq"], "ask_qty_bps25": d25["aq"],
        "bid_notional_bps25": d25["bn"], "ask_notional_bps25": d25["an"], "imbalance_bps25": d25["imb"],
        "bid_qty_bps50": d50["bq"], "ask_qty_bps50": d50["aq"],
        "bid_notional_bps50": d50["bn"], "ask_notional_bps50": d50["an"], "imbalance_bps50": d50["imb"],
        "bid_wall_price": bwp, "bid_wall_qty": bwq, "bid_wall_notional": bwn,
        "bid_wall_bps_dist": bwd, "bid_wall_ratio": bwr,
        "ask_wall_price": awp, "ask_wall_qty": awq, "ask_wall_notional": awn,
        "ask_wall_bps_dist": awd, "ask_wall_ratio": awr,
        "bid_qty_added": bid_qty_added, "bid_qty_removed": bid_qty_removed,
        "ask_qty_added": ask_qty_added, "ask_qty_removed": ask_qty_removed,
        "bid_add_count": bid_add_count, "bid_remove_count": bid_remove_count,
        "ask_add_count": ask_add_count, "ask_remove_count": ask_remove_count,
        "ofi": ofi, "mid_price_change": mid_price_change,
        "imbalance_l10_change": imbalance_l10_change,
        "imbalance_l50_change": imbalance_l50_change,
    }


def _invalid_row(*, exchange: str, market: str, symbol: str, depth: int,
                 bucket_start_ms: int, first_ts_ms: int, last_ts_ms: int,
                 last_u: int, processed_updates: int,
                 quality_flags: list[str]) -> dict[str, Any]:
    from datetime import datetime, timezone

    def ms_to_dt(ms: int) -> datetime:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)

    qf = ",".join(quality_flags)
    z = ZERO
    return {
        "exchange": exchange, "market": market, "symbol": symbol, "depth": depth,
        "bucket_start": ms_to_dt(bucket_start_ms),
        "first_source_ts": ms_to_dt(first_ts_ms),
        "last_source_ts": ms_to_dt(last_ts_ms),
        "last_update_seq": last_u,
        "processed_updates": processed_updates,
        "parser_version": PARSER_VERSION,
        "created_at": datetime.now(timezone.utc),
        "quality_flags": qf, "is_valid": 0,
        "best_bid_price": z, "best_bid_qty": z, "best_ask_price": z, "best_ask_qty": z,
        "mid_price": z, "microprice": z, "spread_abs": z, "spread_bps": z,
        "bid_qty_l5": z, "ask_qty_l5": z, "bid_notional_l5": z, "ask_notional_l5": z, "imbalance_l5": z,
        "bid_qty_l10": z, "ask_qty_l10": z, "bid_notional_l10": z, "ask_notional_l10": z, "imbalance_l10": z,
        "bid_qty_l25": z, "ask_qty_l25": z, "bid_notional_l25": z, "ask_notional_l25": z, "imbalance_l25": z,
        "bid_qty_l50": z, "ask_qty_l50": z, "bid_notional_l50": z, "ask_notional_l50": z, "imbalance_l50": z,
        "bid_qty_bps5": z, "ask_qty_bps5": z, "bid_notional_bps5": z, "ask_notional_bps5": z, "imbalance_bps5": z,
        "bid_qty_bps10": z, "ask_qty_bps10": z, "bid_notional_bps10": z, "ask_notional_bps10": z, "imbalance_bps10": z,
        "bid_qty_bps25": z, "ask_qty_bps25": z, "bid_notional_bps25": z, "ask_notional_bps25": z, "imbalance_bps25": z,
        "bid_qty_bps50": z, "ask_qty_bps50": z, "bid_notional_bps50": z, "ask_notional_bps50": z, "imbalance_bps50": z,
        "bid_wall_price": z, "bid_wall_qty": z, "bid_wall_notional": z, "bid_wall_bps_dist": z, "bid_wall_ratio": z,
        "ask_wall_price": z, "ask_wall_qty": z, "ask_wall_notional": z, "ask_wall_bps_dist": z, "ask_wall_ratio": z,
        "bid_qty_added": None, "bid_qty_removed": None,
        "ask_qty_added": None, "ask_qty_removed": None,
        "bid_add_count": None, "bid_remove_count": None,
        "ask_add_count": None, "ask_remove_count": None,
        "ofi": None, "mid_price_change": None,
        "imbalance_l10_change": None, "imbalance_l50_change": None,
    }

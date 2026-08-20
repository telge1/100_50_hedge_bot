"""Shared V3 second-bucket feature construction (batch parser and live collector).

Do not invent a second feature definition. Both paths call ``compute_features``.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from orderbook_analyse.orderbook_v2.book import ZERO, BookState, sorted_asks, sorted_bids
from orderbook_analyse.orderbook_v2.features import compute_features


def mid_of(book: BookState) -> Decimal | None:
    bids = sorted_bids(book)
    asks = sorted_asks(book)
    if bids and asks:
        return (bids[0][0] + asks[0][0]) / Decimal("2")
    return None


def snapshot_is_usable(book: BookState) -> bool:
    if not book.is_valid:
        return False
    bids = sorted_bids(book)
    asks = sorted_asks(book)
    if not bids or not asks:
        return False
    return bids[0][0] < asks[0][0]


def compute_dynamics(
    delta_events: list[dict[str, Any]],
    prev_book: BookState,
) -> dict[str, Any]:
    """Delta-derived activity metrics for one UTC second."""
    bid_added = ZERO
    bid_removed = ZERO
    ask_added = ZERO
    ask_removed = ZERO
    bid_add_n = 0
    bid_rem_n = 0
    ask_add_n = 0
    ask_rem_n = 0
    ofi = ZERO

    prev_best_bid = max(prev_book.bids.keys(), default=ZERO)
    prev_best_ask = min(prev_book.asks.keys(), default=ZERO)

    for d in delta_events:
        for item in d.get("b") or []:
            p = Decimal(item[0])
            q = Decimal(item[1])
            old_q = prev_book.bids.get(p, ZERO)
            if q == ZERO:
                if old_q > ZERO:
                    bid_removed += old_q
                    bid_rem_n += 1
                    if p == prev_best_bid:
                        ofi -= old_q
            else:
                delta_q = q - old_q
                if delta_q > ZERO:
                    bid_added += delta_q
                    bid_add_n += 1
                else:
                    bid_removed += abs(delta_q)
                    bid_rem_n += 1
                if p == prev_best_bid:
                    ofi += (q - old_q)

        for item in d.get("a") or []:
            p = Decimal(item[0])
            q = Decimal(item[1])
            old_q = prev_book.asks.get(p, ZERO)
            if q == ZERO:
                if old_q > ZERO:
                    ask_removed += old_q
                    ask_rem_n += 1
                    if p == prev_best_ask:
                        ofi += old_q
            else:
                delta_q = q - old_q
                if delta_q > ZERO:
                    ask_added += delta_q
                    ask_add_n += 1
                else:
                    ask_removed += abs(delta_q)
                    ask_rem_n += 1
                if p == prev_best_ask:
                    ofi -= (q - old_q)

    return {
        "bid_qty_added": bid_added,
        "bid_qty_removed": bid_removed,
        "ask_qty_added": ask_added,
        "ask_qty_removed": ask_removed,
        "bid_add_count": bid_add_n,
        "bid_remove_count": bid_rem_n,
        "ask_add_count": ask_add_n,
        "ask_remove_count": ask_rem_n,
        "ofi": ofi,
    }


def zero_dynamics() -> dict[str, Any]:
    """Activity metrics for a carry-forward second: zeros, not None."""
    return {
        "bid_qty_added": ZERO,
        "bid_qty_removed": ZERO,
        "ask_qty_added": ZERO,
        "ask_qty_removed": ZERO,
        "bid_add_count": 0,
        "bid_remove_count": 0,
        "ask_add_count": 0,
        "ask_remove_count": 0,
        "ofi": ZERO,
    }


def build_event_feature_row(
    book: BookState,
    bucket_start_ms: int,
    first_ts_ms: int,
    last_ts_ms: int,
    processed_updates: int,
    *,
    exchange: str,
    market: str,
    symbol: str,
    depth: int,
    quality_flags: list[str] | None,
    delta_data: list[dict[str, Any]],
    book_at_bucket_start: BookState | None,
    prev_mid: Decimal | None,
) -> dict[str, Any]:
    dyn: dict[str, Any] = {}
    mid_change: Decimal | None = None
    if delta_data and book_at_bucket_start is not None:
        dyn = compute_dynamics(delta_data, book_at_bucket_start)
        if prev_mid is not None:
            cur_mid = mid_of(book)
            if cur_mid is not None:
                mid_change = cur_mid - prev_mid
    return compute_features(
        book,
        bucket_start_ms,
        first_ts_ms,
        last_ts_ms,
        processed_updates,
        exchange=exchange,
        market=market,
        symbol=symbol,
        depth=depth,
        quality_flags=quality_flags if quality_flags else None,
        **dyn,
        mid_price_change=mid_change,
        imbalance_l10_change=None,
        imbalance_l50_change=None,
    )


def build_carry_forward_row(
    book: BookState,
    bucket_start_ms: int,
    *,
    exchange: str,
    market: str,
    symbol: str,
    depth: int,
) -> dict[str, Any]:
    return compute_features(
        book,
        bucket_start_ms,
        bucket_start_ms,
        bucket_start_ms,
        processed_updates=0,
        exchange=exchange,
        market=market,
        symbol=symbol,
        depth=depth,
        quality_flags=["carried_forward"],
        **zero_dynamics(),
        mid_price_change=None,
        imbalance_l10_change=None,
        imbalance_l50_change=None,
    )

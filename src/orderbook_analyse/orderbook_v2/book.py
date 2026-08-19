"""In-memory order book: snapshot/delta reconstruction for ob200 format.

Format (per line in .data file):
  {"topic":"orderbook.200.ADAUSDT","type":"snapshot"|"delta","ts":<ms>,"cts":null,
   "data":{"s":"ADAUSDT","b":[["price_str","qty_str"],...],"a":[...],"u":<seq_u>,"seq":<seq>}}

Semantics:
- type=snapshot: full replacement; book["b"] and book["a"] contain ALL levels.
- type=delta: incremental; b/a entries whose qty="0" remove that price level,
  otherwise upsert (insert or update).
- Bids: sorted descending by price (best_bid = highest).
- Asks: sorted ascending by price (best_ask = lowest).
- data.u: monotonically increasing update counter; gaps indicate missing messages.
- data.seq: exchange byte-level sequence counter.

No lookahead: features for bucket T use only events with ts in [T*1000, (T+1)*1000).
The last valid book state within the second is used for snapshot-based metrics.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, NamedTuple

ZERO = Decimal("0")


class BookState(NamedTuple):
    bids: dict[Decimal, Decimal]  # price -> qty
    asks: dict[Decimal, Decimal]
    last_u: int
    last_seq: int
    is_valid: bool


def _parse_levels(raw: list[list[str]]) -> dict[Decimal, Decimal]:
    out: dict[Decimal, Decimal] = {}
    for item in raw:
        price = Decimal(item[0])
        qty = Decimal(item[1])
        if qty > ZERO:
            out[price] = qty
    return out


def apply_snapshot(data: dict[str, Any]) -> BookState:
    bids = _parse_levels(data.get("b") or [])
    asks = _parse_levels(data.get("a") or [])
    return BookState(bids=bids, asks=asks,
                     last_u=data.get("u", 0), last_seq=data.get("seq", 0),
                     is_valid=True)


def apply_delta(prev: BookState, data: dict[str, Any]) -> tuple[BookState, list[str]]:
    """Apply a delta; return (new_state, quality_warnings)."""
    warnings: list[str] = []

    new_u: int = data.get("u", 0)
    new_seq: int = data.get("seq", 0)

    if not prev.is_valid:
        # propagate invalid until next snapshot
        return BookState(bids={}, asks={}, last_u=new_u, last_seq=new_seq,
                         is_valid=False), ["gap_propagated"]

    if new_u != prev.last_u + 1:
        if new_u == prev.last_u:
            warnings.append(f"seq_dup:u={new_u}")
            return prev, warnings
        warnings.append(f"seq_gap:prev={prev.last_u},cur={new_u}")
        return BookState(bids={}, asks={}, last_u=new_u, last_seq=new_seq,
                         is_valid=False), warnings

    bids = dict(prev.bids)
    asks = dict(prev.asks)

    for item in data.get("b") or []:
        price = Decimal(item[0])
        qty = Decimal(item[1])
        if qty == ZERO:
            bids.pop(price, None)
        else:
            bids[price] = qty

    for item in data.get("a") or []:
        price = Decimal(item[0])
        qty = Decimal(item[1])
        if qty == ZERO:
            asks.pop(price, None)
        else:
            asks[price] = qty

    return BookState(bids=bids, asks=asks, last_u=new_u, last_seq=new_seq,
                     is_valid=True), warnings


def sorted_bids(book: BookState) -> list[tuple[Decimal, Decimal]]:
    return sorted(book.bids.items(), key=lambda x: x[0], reverse=True)


def sorted_asks(book: BookState) -> list[tuple[Decimal, Decimal]]:
    return sorted(book.asks.items(), key=lambda x: x[0])

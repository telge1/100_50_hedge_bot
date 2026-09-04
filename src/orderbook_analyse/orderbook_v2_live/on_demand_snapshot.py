"""In-memory on-demand OB1000 snapshot payload builder."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

SOURCE_NAME = "orderbook_v3_live_on_demand"


def _finite(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


def book_levels_from_state(book, *, side: str) -> list[dict[str, Any]]:
    from orderbook_analyse.orderbook_v2.book import sorted_asks, sorted_bids

    levels = sorted_bids(book) if side == "bid" else sorted_asks(book)
    out: list[dict[str, Any]] = []
    for price, size in levels:
        p = _finite(price)
        s = _finite(size)
        if p is None or s is None or s < 0:
            continue
        out.append({"price": p, "size": s, "side": side})
    return out


def build_snapshot_payload(
    *,
    symbol: str,
    depth: int,
    book,
    timestamp_utc: datetime | None,
    subscription_state: str,
    freshness_state: str = "unknown",
    freshness_ms: int | None = None,
    data_status: str = "current",
    data_status_reason: str | None = None,
) -> dict[str, Any]:
    sym = str(symbol).upper()
    bids = book_levels_from_state(book, side="bid")
    asks = book_levels_from_state(book, side="ask")
    if timestamp_utc is None:
        raise ValueError("missing_source_timestamp")
    ts = timestamp_utc
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    best_bid = bids[0]["price"] if bids else None
    best_ask = asks[0]["price"] if asks else None
    if best_bid is not None and best_ask is not None and best_bid >= best_ask:
        raise ValueError("crossed_book")
    mid = None
    if best_bid is not None and best_ask is not None:
        mid = (best_bid + best_ask) / 2.0
    payload: dict[str, Any] = {
        "symbol": sym,
        "depth": depth,
        "timestamp_utc": ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "source": SOURCE_NAME,
        "subscription_state": subscription_state,
        "freshness_state": freshness_state,
        "freshness_ms": freshness_ms,
        "bids": bids,
        "asks": asks,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "bid_levels": len(bids),
        "ask_levels": len(asks),
        "coverage": "on_demand",
        "data_status": data_status,
    }
    if data_status_reason:
        payload["data_status_reason"] = data_status_reason
    return payload

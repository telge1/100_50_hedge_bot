"""Pure Phase-2 transformations shared by pilot and future incremental runs."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from .phase2_contracts import TICK_SIZE


def floor_bucket(value: datetime, milliseconds: int) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    epoch_ms = int(value.timestamp() * 1000)
    floored = epoch_ms - epoch_ms % milliseconds
    return datetime.fromtimestamp(floored / 1000, tz=timezone.utc)


def aggregate_trade_buckets(
    trades: list[dict[str, Any]], milliseconds: int
) -> list[dict[str, Any]]:
    if milliseconds not in {100, 500, 1000}:
        raise ValueError("unsupported trade bucket")
    dedup: dict[str, dict[str, Any]] = {}
    for trade in trades:
        trade_id = str(trade["trade_id"])
        if trade_id in dedup and dedup[trade_id] != trade:
            raise ValueError(f"conflicting trade_id: {trade_id}")
        dedup[trade_id] = trade
    buckets: dict[tuple[str, datetime], dict[str, Any]] = defaultdict(
        lambda: {
            "buy_base_volume": Decimal("0"),
            "sell_base_volume": Decimal("0"),
            "buy_quote_notional": Decimal("0"),
            "sell_quote_notional": Decimal("0"),
            "buy_trade_count": 0,
            "sell_trade_count": 0,
            "first_trade_ts": None,
            "last_trade_ts": None,
            "source_trade_count": 0,
            "deduplicated_trade_count": 0,
        }
    )
    for trade in sorted(dedup.values(), key=lambda row: (row["event_time"], row["trade_id"])):
        key = (str(trade["symbol"]), floor_bucket(trade["event_time"], milliseconds))
        bucket = buckets[key]
        side = str(trade["side"])
        size = Decimal(str(trade["size"]))
        notional = Decimal(str(trade["notional"]))
        if side == "Buy":
            bucket["buy_base_volume"] += size
            bucket["buy_quote_notional"] += notional
            bucket["buy_trade_count"] += 1
        elif side == "Sell":
            bucket["sell_base_volume"] += size
            bucket["sell_quote_notional"] += notional
            bucket["sell_trade_count"] += 1
        else:
            raise ValueError(f"unknown taker side: {side}")
        event_time = trade["event_time"]
        bucket["first_trade_ts"] = min(
            event_time, bucket["first_trade_ts"] or event_time
        )
        bucket["last_trade_ts"] = max(
            event_time, bucket["last_trade_ts"] or event_time
        )
        bucket["source_trade_count"] += 1
        bucket["deduplicated_trade_count"] += 1
    return [
        {
            "symbol": symbol,
            "bucket_start": bucket_start,
            **values,
            "taker_delta_quote_notional": (
                values["buy_quote_notional"] - values["sell_quote_notional"]
            ),
        }
        for (symbol, bucket_start), values in sorted(buckets.items())
    ]


def price_to_tick(symbol: str, price: Decimal) -> int:
    tick = TICK_SIZE[symbol]
    ticks = price / tick
    if ticks != ticks.to_integral_value():
        raise ValueError(f"price is not on {symbol} tick: {price}")
    return int(ticks)


def compact_ob_state(
    symbol: str,
    bids: tuple[tuple[Decimal, Decimal], ...],
    asks: tuple[tuple[Decimal, Decimal], ...],
) -> dict[str, Any]:
    if not bids or not asks or bids[0][0] >= asks[0][0]:
        raise ValueError("invalid order book")
    bid_ticks = [price_to_tick(symbol, price) for price, _ in bids]
    ask_ticks = [price_to_tick(symbol, price) for price, _ in asks]
    if bid_ticks != sorted(bid_ticks, reverse=True):
        raise ValueError("bids not descending")
    if ask_ticks != sorted(ask_ticks):
        raise ValueError("asks not ascending")
    best_bid, best_ask = bids[0][0], asks[0][0]
    return {
        "bid_price_ticks": bid_ticks,
        "bid_quantities": [qty for _, qty in bids],
        "ask_price_ticks": ask_ticks,
        "ask_quantities": [qty for _, qty in asks],
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": (best_bid + best_ask) / Decimal("2"),
        "spread": best_ask - best_bid,
        "bid_level_count": len(bids),
        "ask_level_count": len(asks),
        "genuine_depth": int(len(bids) == 200 and len(asks) == 200),
    }


def assert_no_carry_after_terminal(
    snapshot_time: datetime, terminal: datetime, is_carried_forward: bool
) -> None:
    if snapshot_time > terminal and is_carried_forward:
        raise ValueError("carried-forward across producer terminal forbidden")

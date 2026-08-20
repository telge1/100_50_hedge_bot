"""5s trade-flow aggregation helpers (smoke / audits)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

from orderbook_analyse.public_trade_source.protocol import NormalizedPublicTrade


def floor_5s(ts: datetime) -> datetime:
    ts = ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc)
    epoch = int(ts.timestamp())
    floored = epoch - (epoch % 5)
    return datetime.fromtimestamp(floored, tz=timezone.utc)


def aggregate_trade_flow_5s(
    trades: Iterable[NormalizedPublicTrade],
) -> list[dict]:
    buckets: dict[datetime, dict] = {}
    for t in trades:
        b = floor_5s(t.trade_ts)
        if b not in buckets:
            buckets[b] = {
                "bucket_ts": b,
                "buy_count": 0,
                "sell_count": 0,
                "buy_size": Decimal("0"),
                "sell_size": Decimal("0"),
                "buy_notional": Decimal("0"),
                "sell_notional": Decimal("0"),
                "last_price": t.price,
            }
        row = buckets[b]
        if t.side == "Buy":
            row["buy_count"] += 1
            row["buy_size"] += t.size
            row["buy_notional"] += t.notional
        else:
            row["sell_count"] += 1
            row["sell_size"] += t.size
            row["sell_notional"] += t.notional
        row["last_price"] = t.price

    out: list[dict] = []
    for b in sorted(buckets):
        row = buckets[b]
        buy_n = row["buy_notional"]
        sell_n = row["sell_notional"]
        total = buy_n + sell_n
        out.append(
            {
                "bucket_ts": b.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "buy_count": row["buy_count"],
                "sell_count": row["sell_count"],
                "buy_size": format(row["buy_size"], "f"),
                "sell_size": format(row["sell_size"], "f"),
                "buy_notional": format(buy_n, "f"),
                "sell_notional": format(sell_n, "f"),
                "delta_notional": format(buy_n - sell_n, "f"),
                "total_notional": format(total, "f"),
                "last_price": format(row["last_price"], "f"),
            }
        )
    return out

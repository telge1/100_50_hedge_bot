"""Trade and liquidation bucketing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from research.btc_ob_fight.config import iso_z, utc
from research.btc_ob_fight.facts import window_trade_facts


def bucket_trades(
    trades: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
    seconds: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    t = utc(start)
    end = utc(end)
    while t < end:
        te = t + timedelta(seconds=seconds)
        w = window_trade_facts(trades, t, te, label=f"{seconds}s")
        rows.append(
            {
                "bucket_start": iso_z(t),
                "bucket_end": iso_z(te),
                "bucket_seconds": seconds,
                **{k: w[k] for k in w if k != "label"},
            }
        )
        t = te
    return rows


def bucket_liquidations(
    events: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
    seconds: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    t = utc(start)
    end = utc(end)
    while t < end:
        te = t + timedelta(seconds=seconds)
        ts_start = iso_z(t)
        ts_end = iso_z(te)
        chunk = []
        for e in events:
            et = datetime.fromisoformat(e["event_time"].replace("Z", "+00:00"))
            if t <= et < te:
                chunk.append(e)
        short = [e for e in chunk if e["liquidated_side"] == "LIQUIDATED_SHORT"]
        long_ = [e for e in chunk if e["liquidated_side"] == "LIQUIDATED_LONG"]
        rows.append(
            {
                "bucket_start": ts_start,
                "bucket_end": ts_end,
                "bucket_seconds": seconds,
                "short_liquidation_count": len(short),
                "short_liquidation_base": sum(e["base_volume"] for e in short),
                "short_liquidation_quote": sum(e["quote_notional"] for e in short),
                "long_liquidation_count": len(long_),
                "long_liquidation_base": sum(e["base_volume"] for e in long_),
                "long_liquidation_quote": sum(e["quote_notional"] for e in long_),
                "largest_event_notional": max((e["quote_notional"] for e in chunk), default=0),
                "cumulative_short_quote_in_bucket": sum(e["quote_notional"] for e in short),
            }
        )
        t = te
    return rows

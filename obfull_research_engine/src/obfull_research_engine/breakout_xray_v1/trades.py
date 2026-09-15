"""Public-trade deduplication and multi-horizon flow (offline-safe).

Dedup key matches outcomes.public_trade_index: trade_id, sort (trade_ts, trade_id).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence


# Parity with PublicTradeIndex / DedupReport
DEDUP_KEY = "trade_id"
SORT_KEYS = ("trade_ts", "trade_id")


@dataclass(frozen=True)
class XRayTrade:
    trade_ts: datetime
    trade_id: str
    side: str
    price: float
    size: float
    notional: float

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["trade_ts"] = self.trade_ts.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        return d


@dataclass(frozen=True)
class DedupStats:
    raw_rows: int
    unique_trades: int
    duplicate_rows: int
    duplicate_ratio: float
    largest_buy: dict[str, Any] | None
    largest_sell: dict[str, Any] | None
    # back-compat aliases used by Phase-1 tests
    unique_trade_ids: int = 0
    duplicates_dropped: int = 0
    dedup_key: str = DEDUP_KEY
    sort_keys: tuple[str, ...] = SORT_KEYS

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_rows": self.raw_rows,
            "unique_trades": self.unique_trades,
            "unique_trade_ids": self.unique_trade_ids or self.unique_trades,
            "duplicate_rows": self.duplicate_rows,
            "duplicates_dropped": self.duplicates_dropped or self.duplicate_rows,
            "duplicate_ratio": self.duplicate_ratio,
            "largest_buy": self.largest_buy,
            "largest_sell": self.largest_sell,
            "dedup_key": self.dedup_key,
            "sort_keys": list(self.sort_keys),
        }


def dedup_trades_by_id(
    trades: Sequence[XRayTrade], *, raw_count: int | None = None
) -> tuple[list[XRayTrade], DedupStats]:
    seen: set[str] = set()
    out: list[XRayTrade] = []
    dup = 0
    ordered = sorted(
        trades, key=lambda t: (t.trade_ts.astimezone(timezone.utc), t.trade_id)
    )
    for t in ordered:
        if t.trade_id in seen:
            dup += 1
            continue
        seen.add(t.trade_id)
        out.append(t)
    raw = int(raw_count if raw_count is not None else len(trades))
    buys = [t for t in out if t.side == "Buy"]
    sells = [t for t in out if t.side == "Sell"]
    lb = max(buys, key=lambda t: t.notional, default=None)
    ls = max(sells, key=lambda t: t.notional, default=None)
    stats = DedupStats(
        raw_rows=raw,
        unique_trades=len(out),
        duplicate_rows=dup,
        duplicate_ratio=(dup / raw) if raw else 0.0,
        largest_buy=None if lb is None else lb.to_dict(),
        largest_sell=None if ls is None else ls.to_dict(),
        unique_trade_ids=len(out),
        duplicates_dropped=dup,
    )
    return out, stats


def flow_summary(trades: Sequence[XRayTrade]) -> dict[str, Any]:
    buy = sum(t.notional for t in trades if t.side == "Buy")
    sell = sum(t.notional for t in trades if t.side == "Sell")
    tot = buy + sell
    buys = [t for t in trades if t.side == "Buy"]
    sells = [t for t in trades if t.side == "Sell"]
    max_buy = max((t.notional for t in buys), default=0.0)
    max_sell = max((t.notional for t in sells), default=0.0)
    largest_buy = max(buys, key=lambda t: t.notional, default=None)
    largest_sell = max(sells, key=lambda t: t.notional, default=None)
    return {
        "trades": len(trades),
        "buy_notional": buy,
        "sell_notional": sell,
        "delta": buy - sell,
        "imbalance": ((buy - sell) / tot) if tot else None,
        "max_buy_notional": max_buy,
        "max_sell_notional": max_sell,
        "largest_buy_trade_id": None if largest_buy is None else largest_buy.trade_id,
        "largest_buy_trade": None if largest_buy is None else largest_buy.to_dict(),
        "largest_sell_trade_id": None if largest_sell is None else largest_sell.trade_id,
        "largest_sell_trade": None if largest_sell is None else largest_sell.to_dict(),
        "large_trade_share_buy": (max_buy / buy) if buy else None,
    }


def aggregate_flow_horizons(
    trades: Sequence[XRayTrade],
    *,
    origin: datetime,
    horizons_s: Iterable[int] = (1, 5, 15, 30, 60, 120, 300),
) -> dict[str, Any]:
    """Half-open aggregates on [origin, origin+H)."""
    origin = origin.astimezone(timezone.utc)
    origin_ts = origin.timestamp()
    out: dict[str, Any] = {}
    for h in horizons_s:
        end_ts = origin_ts + float(h)
        window = [
            t
            for t in trades
            if origin_ts <= t.trade_ts.astimezone(timezone.utc).timestamp() < end_ts
        ]
        summary = flow_summary(window)
        summary["horizon_s"] = h
        summary["span"] = f"[origin, origin+{h}s)"
        out[f"{h}s"] = summary
    return out


def price_move_per_delta_million(
    price_move: float, delta_notional: float
) -> float | None:
    if abs(delta_notional) < 1e-12:
        return None
    return float(price_move) / (float(delta_notional) / 1_000_000.0)

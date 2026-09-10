"""Read-only Public Trade index for episode_outcome_public_trade_v1."""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.research.general_market_behavior_v1.coverage import load_clickhouse_env


def _q(sql: str) -> str:
    host = os.environ.get("CLICKHOUSE_HOST", "127.0.0.1")
    port = os.environ.get("CLICKHOUSE_HTTP_PORT") or os.environ.get("CLICKHOUSE_PORT") or "8123"
    user = os.environ.get("CLICKHOUSE_USER", "default")
    password = os.environ.get("CLICKHOUSE_PASSWORD", "")
    params = {"user": user}
    if password:
        params["password"] = password
    url = f"http://{host}:{port}/?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, data=sql.encode(), method="POST")
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.read().decode()


def _parse_ch_dt(value: str) -> datetime:
    v = value.strip()
    if v.endswith("Z"):
        return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(timezone.utc)
    if "." in v:
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                return datetime.strptime(v[:26] if len(v) > 26 else v, "%Y-%m-%d %H:%M:%S.%f").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue
    return datetime.strptime(v[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class PublicTrade:
    trade_ts: pd.Timestamp
    ingest_timestamp: pd.Timestamp | None
    trade_id: str
    price: float
    side: str
    size: float
    notional: float


@dataclass
class DedupReport:
    raw_rows: int = 0
    unique_trade_ids: int = 0
    duplicates_dropped: int = 0
    sort_keys: tuple[str, ...] = ("trade_ts", "trade_id")
    dedup_key: str = "trade_id"
    ambiguous_timestamp_groups: int = 0


class PublicTradeIndex:
    """Sorted, deduplicated public trades keyed by event time."""

    def __init__(self, trades: list[PublicTrade], *, dedup: DedupReport | None = None):
        self.trades = list(trades)
        self.dedup = dedup or DedupReport(raw_rows=len(trades), unique_trade_ids=len(trades))
        self._ts = np.array([t.trade_ts.value for t in self.trades], dtype=np.int64) if self.trades else np.array([], dtype=np.int64)
        self.first_trade_ts = self.trades[0].trade_ts if self.trades else None
        self.last_trade_ts = self.trades[-1].trade_ts if self.trades else None
        # timestamp collision groups (same trade_ts, >1 id) after dedup
        amb = 0
        if self.trades:
            prev = None
            n = 0
            for t in self.trades:
                if prev is not None and t.trade_ts == prev:
                    n += 1
                else:
                    if n > 1:
                        amb += 1
                    n = 1
                    prev = t.trade_ts
            if n > 1:
                amb += 1
        self.dedup.ambiguous_timestamp_groups = amb

    @classmethod
    def from_trades(cls, trades: list[PublicTrade], *, raw_count: int | None = None) -> "PublicTradeIndex":
        seen: set[str] = set()
        out: list[PublicTrade] = []
        dup = 0
        for t in sorted(trades, key=lambda x: (x.trade_ts.value, x.trade_id)):
            if t.trade_id in seen:
                dup += 1
                continue
            seen.add(t.trade_id)
            out.append(t)
        report = DedupReport(
            raw_rows=int(raw_count if raw_count is not None else len(trades)),
            unique_trade_ids=len(out),
            duplicates_dropped=dup,
        )
        return cls(out, dedup=report)

    def latest_before(
        self,
        cut: pd.Timestamp,
        *,
        max_age_ms: int,
        strict: bool = True,
    ) -> tuple[PublicTrade | None, str | None]:
        """Last trade with trade_ts < cut (strict) or <= cut.

        Returns (trade, reject_reason) where reject_reason is None if ok,
        'MISSING' if none, 'STALE' if age exceeded.
        """
        cut = pd.Timestamp(cut).tz_convert("UTC")
        if not len(self._ts):
            return None, "MISSING"
        tval = int(cut.value)
        if strict:
            idx = int(np.searchsorted(self._ts, tval, side="left") - 1)
        else:
            idx = int(np.searchsorted(self._ts, tval, side="right") - 1)
        if idx < 0:
            return None, "MISSING"
        tr = self.trades[idx]
        age_ms = (cut - tr.trade_ts).total_seconds() * 1000.0
        if age_ms > float(max_age_ms):
            return tr, "STALE"
        return tr, None

    def path_slice(self, start: pd.Timestamp, end: pd.Timestamp) -> list[PublicTrade]:
        """Trades with start <= trade_ts <= end."""
        start = pd.Timestamp(start).tz_convert("UTC")
        end = pd.Timestamp(end).tz_convert("UTC")
        if not len(self._ts):
            return []
        lo = int(np.searchsorted(self._ts, int(start.value), side="left"))
        hi = int(np.searchsorted(self._ts, int(end.value), side="right"))
        return self.trades[lo:hi]


def load_public_trades_window(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
) -> tuple[PublicTradeIndex, dict[str, Any]]:
    """Load canonical public trades [start, end) read-only from ClickHouse."""
    load_clickhouse_env()
    symbol = symbol.upper()
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    hs = start.strftime("%Y-%m-%d %H:%M:%S")
    he = end.strftime("%Y-%m-%d %H:%M:%S")
    sql = (
        "SELECT trade_ts, ingest_timestamp, trade_id, side, price, size, notional, source "
        "FROM orderbook_analysis.public_trades_canonical "
        f"WHERE symbol='{symbol}' AND trade_ts>='{hs}' AND trade_ts<'{he}' "
        "ORDER BY trade_ts, trade_id FORMAT JSONEachRow"
    )
    raw = _q(sql)
    raw_rows = 0
    parsed: list[PublicTrade] = []
    sources: dict[str, int] = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        raw_rows += 1
        o = json.loads(line)
        src = str(o.get("source") or "")
        sources[src] = sources.get(src, 0) + 1
        price = float(o["price"])
        size = float(o["size"])
        ing = o.get("ingest_timestamp")
        parsed.append(
            PublicTrade(
                trade_ts=pd.Timestamp(_parse_ch_dt(str(o["trade_ts"]))).tz_convert("UTC"),
                ingest_timestamp=pd.Timestamp(_parse_ch_dt(str(ing))).tz_convert("UTC") if ing else None,
                trade_id=str(o["trade_id"]),
                price=price,
                side=str(o.get("side") or ""),
                size=size,
                notional=float(o.get("notional") or price * size),
            )
        )
    idx = PublicTradeIndex.from_trades(parsed, raw_count=raw_rows)
    tip = _q(
        f"SELECT max(trade_ts) FROM orderbook_analysis.public_trades_canonical WHERE symbol='{symbol}' FORMAT TSV"
    ).strip()
    meta = {
        "symbol": symbol,
        "load_start": start.isoformat().replace("+00:00", "Z"),
        "load_end_exclusive": end.isoformat().replace("+00:00", "Z"),
        "ch_table": "orderbook_analysis.public_trades_canonical",
        "raw_rows": raw_rows,
        "unique_trade_ids": idx.dedup.unique_trade_ids,
        "duplicates_dropped": idx.dedup.duplicates_dropped,
        "source_counts": sources,
        "first_trade_ts": idx.first_trade_ts.isoformat().replace("+00:00", "Z") if idx.first_trade_ts is not None else None,
        "last_trade_ts": idx.last_trade_ts.isoformat().replace("+00:00", "Z") if idx.last_trade_ts is not None else None,
        "ch_tip_trade_ts": tip or None,
        "empty_second_policy": "NOT_AUTOMATICALLY_SOURCE_GAP",
    }
    return idx, meta

"""Research Charts: causal public-trade bubbles (layer-only, no scanner jobs).

Mirrors orderbook_analyse.public_trade_bubbles aggregation against
orderbook_analysis.public_trades_canonical. Read-only.
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import clickhouse_connect
from clickhouse_connect.driver.exceptions import DatabaseError, OperationalError

from .clickhouse_config import load_clickhouse_config

CANONICAL_FQN = "orderbook_analysis.public_trades_canonical"
TIME_BUCKET_S = 1
PRICE_TICKS = 5
SIZE_LOOKBACK = 300
SIZE_WARMUP = 40
Q_MED, Q_LRG, Q_EXT = 0.70, 0.90, 0.97
MAX_RANGE_S = 6 * 3600
WARMUP_S = 20 * 60
QUERY_TIMEOUT_S = 20
QUERY_MEMORY = 200_000_000
_CACHE_MAX = 48
_CACHE_TTL = 3.0

_cache_lock = threading.Lock()
_cache: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()


def _client():
    return clickhouse_connect.get_client(**load_clickhouse_config().connect_kwargs())


def _settings() -> dict[str, int]:
    return {
        "max_execution_time": QUERY_TIMEOUT_S,
        "max_memory_usage": QUERY_MEMORY,
        "max_threads": 2,
    }


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def tick_size(symbol: str) -> float:
    sym = str(symbol or "").upper()
    if sym in {"DOGEUSDT", "1000PEPEUSDT", "1000BONKUSDT"}:
        return 1e-5
    if sym == "BTCUSDT":
        return 0.1
    if sym in {"ETHUSDT", "BNBUSDT", "SOLUSDT"}:
        return 0.01
    return 1e-4


def _quantile(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    vals = sorted(vals)
    if len(vals) == 1:
        return vals[0]
    idx = min(1.0, max(0.0, q)) * (len(vals) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return float(vals[lo])
    w = idx - lo
    return float(vals[lo] * (1 - w) + vals[hi] * w)


def classify_size(total: float, prior: list[float]) -> tuple[str, dict[str, Any]]:
    meta: dict[str, Any] = {
        "sample_count": len(prior),
        "threshold_medium": None,
        "threshold_large": None,
        "threshold_extreme": None,
    }
    if len(prior) < SIZE_WARMUP:
        return "UNCALIBRATED", meta
    t_m = _quantile(prior, Q_MED)
    t_l = _quantile(prior, Q_LRG)
    t_e = _quantile(prior, Q_EXT)
    meta.update(threshold_medium=t_m, threshold_large=t_l, threshold_extreme=t_e)
    if t_e is not None and total >= t_e:
        return "EXTREME", meta
    if t_l is not None and total >= t_l:
        return "LARGE", meta
    if t_m is not None and total >= t_m:
        return "MEDIUM", meta
    return "SMALL", meta


def _iso(dt: datetime) -> str:
    return _utc(dt).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def load_trades(symbol: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    sym = str(symbol).strip().upper()
    start, end = _utc(start), _utc(end)
    # Allow warmup lookback beyond the visible-range cap (checked by caller).
    if (end - start).total_seconds() > MAX_RANGE_S + WARMUP_S + 60:
        raise ValueError("time_range_too_large")
    client = _client()
    try:
        rows = client.query(
            f"""
            SELECT
              trade_ts, trade_id, side,
              toFloat64(price) AS price,
              toFloat64(size) AS size,
              toFloat64(notional) AS notional,
              source, ingest_timestamp
            FROM {CANONICAL_FQN} FINAL
            PREWHERE symbol = {{s:String}}
            WHERE trade_ts >= {{a:DateTime64(3,'UTC')}}
              AND trade_ts < {{b:DateTime64(3,'UTC')}}
            ORDER BY trade_ts, trade_id
            """,
            parameters={"s": sym, "a": start, "b": end},
            settings=_settings(),
        ).result_rows
    except (DatabaseError, OperationalError) as exc:
        raise RuntimeError(f"query_failed:{exc}") from exc
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ts, tid, side, price, size, notional, source, ingest in rows:
        tid_s = str(tid)
        if tid_s in seen:
            continue
        seen.add(tid_s)
        if getattr(ts, "tzinfo", None) is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out.append(
            {
                "trade_id": tid_s,
                "trade_ts": ts,
                "side": str(side),
                "price": float(price),
                "size": float(size),
                "notional": float(notional if notional is not None else price * size),
                "source": str(source or ""),
                "received_at": ingest,
            }
        )
    return out


def aggregate(
    trades: list[dict[str, Any]],
    *,
    symbol: str,
    as_of: datetime,
    mode: str = "large_medium",
) -> list[dict[str, Any]]:
    as_of = _utc(as_of)
    tick = tick_size(symbol)
    step = tick * PRICE_TICKS
    buckets: dict[tuple[int, int], dict[str, float]] = {}
    for t in trades:
        ts = t["trade_ts"]
        if getattr(ts, "tzinfo", None) is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts > as_of:
            continue
        epoch = int(ts.timestamp())
        t0 = (epoch // TIME_BUCKET_S) * TIME_BUCKET_S
        p0 = int(math.floor(t["price"] / step + 1e-12)) if step > 0 else 0
        key = (t0, p0)
        b = buckets.get(key)
        if b is None:
            b = {"buy": 0.0, "sell": 0.0, "count": 0.0, "max": 0.0, "pw": 0.0, "ps": 0.0}
            buckets[key] = b
        n = float(t["notional"])
        if t["side"] == "Buy":
            b["buy"] += n
        elif t["side"] == "Sell":
            b["sell"] += n
        b["count"] += 1
        b["max"] = max(b["max"], n)
        b["ps"] += t["price"] * n
        b["pw"] += n

    keys = sorted(buckets.keys())
    as_of_epoch = int(as_of.timestamp())
    closed_timeline: list[tuple[int, float]] = []
    for t0, p0 in keys:
        end_e = t0 + TIME_BUCKET_S
        if end_e > as_of_epoch:
            continue
        st = buckets[(t0, p0)]
        closed_timeline.append((end_e, st["buy"] + st["sell"]))
    closed_timeline.sort()

    by_time: dict[int, list[int]] = defaultdict(list)
    for t0, p0 in keys:
        by_time[t0].append(p0)

    prior: list[float] = []
    pi = 0
    bubbles: list[dict[str, Any]] = []
    for t0 in sorted(by_time.keys()):
        end_e = t0 + TIME_BUCKET_S
        forming = end_e > as_of_epoch
        start_dt = datetime.fromtimestamp(t0, tz=timezone.utc)
        end_dt = datetime.fromtimestamp(end_e, tz=timezone.utc)
        while pi < len(closed_timeline) and closed_timeline[pi][0] <= t0:
            prior.append(closed_timeline[pi][1])
            pi += 1
            if len(prior) > SIZE_LOOKBACK:
                prior = prior[-SIZE_LOOKBACK:]
        look = list(prior)
        for p0 in sorted(by_time[t0]):
            st = buckets[(t0, p0)]
            buy, sell = st["buy"], st["sell"]
            total = buy + sell
            if total <= 0 and st["count"] <= 0:
                continue
            price = (st["ps"] / st["pw"]) if st["pw"] > 0 else (p0 + 0.5) * step
            dom = "BUY" if buy > sell else ("SELL" if sell > buy else "FLAT")
            size_class, meta = classify_size(total, look)
            known = as_of if forming else end_dt
            bubbles.append(
                {
                    "bubble_id": f"{symbol}|{t0}|{p0}|{TIME_BUCKET_S}|{PRICE_TICKS}",
                    "symbol": symbol,
                    "bucket_start": _iso(start_dt),
                    "bucket_end": _iso(end_dt),
                    "timestamp": int(t0),
                    "price": price,
                    "buy_notional": buy,
                    "sell_notional": sell,
                    "total_notional": total,
                    "delta_notional": buy - sell,
                    "trade_count": int(st["count"]),
                    "max_single_trade_notional": st["max"],
                    "dominant_side": dom,
                    "size_class": size_class,
                    "known_at": _iso(known),
                    "forming": forming,
                    "sample_count": meta["sample_count"],
                    "threshold_medium": meta["threshold_medium"],
                    "threshold_large": meta["threshold_large"],
                    "threshold_extreme": meta["threshold_extreme"],
                    "max_feature_timestamp": _iso(known),
                    "research_only": True,
                }
            )

    mode_l = str(mode or "large_medium").lower()
    if mode_l in ("off", "none", ""):
        return []
    if mode_l == "large":
        bubbles = [b for b in bubbles if b["size_class"] in ("LARGE", "EXTREME")]
    elif mode_l in ("large_medium", "large+medium"):
        bubbles = [b for b in bubbles if b["size_class"] in ("MEDIUM", "LARGE", "EXTREME")]
    # all / delta_debug keep all
    return bubbles


def load_bubbles_payload(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    as_of: datetime | None = None,
    mode: str = "large_medium",
) -> dict[str, Any]:
    symbol = str(symbol).strip().upper()
    start, end = _utc(start), _utc(end)
    as_of = _utc(as_of or end)
    if (end - start).total_seconds() > MAX_RANGE_S:
        raise ValueError("time_range_too_large")
    # warm-up for size class (does not count against visible MAX_RANGE_S)
    warm = start - timedelta(seconds=WARMUP_S)
    key = (symbol, int(warm.timestamp()), int(end.timestamp()), int(as_of.timestamp()), mode)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now < hit[0]:
            out = dict(hit[1])
            out["cached"] = True
            return out
    trades = load_trades(symbol, warm, end)
    bubbles = aggregate(trades, symbol=symbol, as_of=as_of, mode=mode)
    # drop warm-only (before visible start) for display, keep size class causal
    start_epoch = int(start.timestamp())
    visible = [b for b in bubbles if int(b["timestamp"]) >= start_epoch]
    payload = {
        "success": True,
        "symbol": symbol,
        "start": _iso(start),
        "end": _iso(end),
        "as_of": _iso(as_of),
        "mode": mode,
        "time_bucket_s": TIME_BUCKET_S,
        "price_ticks_per_bucket": PRICE_TICKS,
        "tick_size": tick_size(symbol),
        "n_trades": len(trades),
        "n_bubbles": len(visible),
        "bubbles": visible,
        "source": CANONICAL_FQN,
        "layer_only": True,
        "research_only": True,
        "cached": False,
    }
    with _cache_lock:
        _cache[key] = (now + _CACHE_TTL, dict(payload))
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return payload

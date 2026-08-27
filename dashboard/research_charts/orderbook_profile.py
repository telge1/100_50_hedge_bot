"""Current Orderbook Walls for Research Charts (read-only).

Primary (when local OB200 archive covers as-of):
  Multi-level walls from closed ``*_ob200_v3.zst`` segments
  (``research_charts.ob200_walls``) — significant levels vs median qty.

Fallback (no OB200 coverage):
  orderbook_analysis.orderbook_features_1s_v2 — one dominant Bid + one Ask
  wall per 1s bucket.

Never invents price levels. Never reads broken ``orderbook_deltas``.

Default UI semantics (``snapshot_at``):
  - Causal book/walls at ``at`` (chart right edge / now).
  - Prefer OB200 multi-walls when archive exists for the symbol/time.
  - Never returns future data relative to ``at`` or ``end``.

Optional ``mode=history``: features-table visible-range mashup (not UI default).
``mode=ob200``: force OB200 path (error if missing).
``mode=features``: force features_1s_v2 dominant walls.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import clickhouse_connect
from clickhouse_connect.driver.exceptions import DatabaseError, OperationalError

from .clickhouse_config import load_clickhouse_config
from .ob200_walls import (
    Ob200WallsError,
    has_ob200_archive,
    load_ob200_walls,
)

FEATURES_FQN = "orderbook_analysis.orderbook_features_1s_v2"
OB200_SOURCE = "ob200_raw_shadow_v3"
MAX_RANGE_SECONDS = 7 * 24 * 3600
MAX_BARS_PER_SIDE = 100
TOP_BY_NOTIONAL = 60
EDGE_BY_PRICE = 30
QUERY_TIMEOUT_S = 8
QUERY_MEMORY_BYTES = 120_000_000
QUERY_THREADS = 2
_CACHE_MAX = 64
_HIST_TTL = 45.0
_LIVE_TTL = 3.0

_ZERO = Decimal("0")
_cache_lock = threading.Lock()
_profile_cache: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()


class OrderbookProfileQueryError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 503):
        super().__init__(message)
        self.code = code
        self.status = status


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def unix_utc(ts: datetime) -> int:
    return int(_utc(ts).timestamp())


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not d.is_finite():
        return None
    return d


def _f(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _client():
    cfg = load_clickhouse_config()
    return clickhouse_connect.get_client(**cfg.connect_kwargs())


def _qsettings() -> dict[str, int]:
    return {
        "max_execution_time": QUERY_TIMEOUT_S,
        "max_memory_usage": QUERY_MEMORY_BYTES,
        "max_threads": QUERY_THREADS,
    }


def _translate_ch(exc: Exception) -> OrderbookProfileQueryError:
    text = str(exc)
    if "241" in text or "MEMORY_LIMIT" in text:
        return OrderbookProfileQueryError(
            "query_memory", "ClickHouse query memory limit exceeded", status=503
        )
    if "TIMEOUT" in text.upper() or "max_execution_time" in text or "159" in text:
        return OrderbookProfileQueryError(
            "query_timeout", "ClickHouse query timeout", status=503
        )
    return OrderbookProfileQueryError("query_failed", "ClickHouse query failed", status=503)


def _cache_get(key: tuple) -> dict | None:
    now = time.monotonic()
    with _cache_lock:
        item = _profile_cache.get(key)
        if not item:
            return None
        expires, value = item
        if now >= expires:
            _profile_cache.pop(key, None)
            return None
        _profile_cache.move_to_end(key)
        out = dict(value)
        out["cached"] = True
        return out


def _cache_put(key: tuple, value: dict, ttl: float) -> None:
    with _cache_lock:
        _profile_cache[key] = (time.monotonic() + ttl, dict(value))
        _profile_cache.move_to_end(key)
        while len(_profile_cache) > _CACHE_MAX:
            _profile_cache.popitem(last=False)


def clear_orderbook_profile_cache_for_tests() -> None:
    with _cache_lock:
        _profile_cache.clear()


def empty_profile(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    mode: str,
    warning: str | None = None,
    profile_kind: str = "current_orderbook_walls",
    label: str = "Current Orderbook Walls",
    source_table: str = FEATURES_FQN,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "success": True,
        "profile_kind": profile_kind,
        "label": label,
        "source_table": source_table,
        "symbol": symbol,
        "mode": mode,
        "start": unix_utc(start),
        "end": unix_utc(end),
        "value_unit": "notional_quote",
        "qty_unit": "base",
        "bars": [],
        "bar_count": 0,
        "bid_count": 0,
        "ask_count": 0,
        "warning": warning,
        "notes": notes
        or [
            "Latest 1s aggregate only: one dominant Bid wall and one dominant Ask wall.",
            "Not a history mashup of older wall prices across the visible range.",
            "Not a full L2 orderbook or OB200 heatmap.",
        ],
        "cached": False,
    }


def _ob200_walls_to_bars(symbol: str, walls: list[dict[str, Any]], *, as_of: datetime, mid: Decimal) -> list[dict[str, Any]]:
    bars: list[dict[str, Any]] = []
    for w in walls:
        bar = _bar_dict(
            side=str(w["side"]),
            price=w["price"],
            value=w["notional"],
            qty=w["qty"],
            reference_price=mid,
            distance_bps=w.get("distance_bps"),
            timestamp=_utc(as_of),
            carried_forward=False,
            samples=1,
            quality_flags="ob200_multi_wall",
        )
        if bar is None:
            continue
        bar["symbol"] = symbol
        bar["wall_ratio"] = float(w.get("ratio") or 0)
        bars.append(bar)
    # Stable draw order: bids low→high then asks low→high (price ascending).
    bars.sort(key=lambda b: (0 if b["side"] == "BID" else 1, b["price"] or 0))
    return bars


def _bar_dict(
    *,
    side: str,
    price: Decimal,
    value: Decimal,
    qty: Decimal | None,
    reference_price: Decimal | None,
    distance_bps: Decimal | None,
    timestamp: datetime,
    carried_forward: bool,
    samples: int,
    quality_flags: str,
) -> dict[str, Any] | None:
    if price is None or price <= _ZERO:
        return None
    if value is None or value < _ZERO:
        return None
    ref = reference_price if reference_price and reference_price > _ZERO else None
    dist_abs = None
    dist_bps = _f(distance_bps) if distance_bps is not None else None
    if ref is not None:
        dist_abs = _f(abs(price - ref))
        if dist_bps is None and ref > _ZERO:
            dist_bps = float((abs(price - ref) / ref) * Decimal("10000"))
    return {
        "timestamp": unix_utc(timestamp),
        "symbol": None,  # filled by caller
        "side": side,
        "price": _f(price),
        "value": _f(value),
        "value_type": "notional_quote",
        "qty": _f(qty),
        "qty_unit": "base",
        "reference_price": _f(ref),
        "distance_abs": dist_abs,
        "distance_bps": dist_bps,
        "carried_forward": bool(carried_forward),
        "quality_flags": quality_flags or "",
        "samples": int(samples),
    }


def _rows_to_bars(symbol: str, rows: list[tuple], *, side: str) -> list[dict[str, Any]]:
    """Map aggregated SQL rows → bar dicts.

    Expected columns:
      price, value(notional), qty, mid, bps_dist, last_ts, samples, cf_samples, quality_flags
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        price = _dec(row[0])
        value = _dec(row[1]) or _ZERO
        qty = _dec(row[2])
        mid = _dec(row[3])
        bps = _dec(row[4])
        ts = row[5]
        samples = int(row[6] or 0)
        cf_samples = int(row[7] or 0)
        flags = str(row[8] or "")
        if not isinstance(ts, datetime):
            continue
        bar = _bar_dict(
            side=side,
            price=price,  # type: ignore[arg-type]
            value=value,
            qty=qty,
            reference_price=mid,
            distance_bps=bps,
            timestamp=_utc(ts),
            carried_forward=cf_samples > 0 or ("carried_forward" in flags),
            samples=samples,
            quality_flags=flags,
        )
        if bar is None:
            continue
        bar["symbol"] = symbol
        out.append(bar)
    return out


def _snapshot_to_bars(symbol: str, row: tuple | None) -> list[dict[str, Any]]:
    if not row:
        return []
    (
        ts,
        mid,
        bid_px,
        bid_qty,
        bid_notional,
        bid_bps,
        ask_px,
        ask_qty,
        ask_notional,
        ask_bps,
        flags,
        _is_valid,
    ) = row
    flags_s = str(flags or "")
    carried = "carried_forward" in flags_s
    ts_u = _utc(ts) if isinstance(ts, datetime) else datetime.now(timezone.utc)
    mid_d = _dec(mid)
    bars: list[dict[str, Any]] = []
    for side, px, qty, notional, bps in (
        ("BID", bid_px, bid_qty, bid_notional, bid_bps),
        ("ASK", ask_px, ask_qty, ask_notional, ask_bps),
    ):
        bar = _bar_dict(
            side=side,
            price=_dec(px),  # type: ignore[arg-type]
            value=_dec(notional) or _ZERO,
            qty=_dec(qty),
            reference_price=mid_d,
            distance_bps=_dec(bps),
            timestamp=ts_u,
            carried_forward=carried,
            samples=1,
            quality_flags=flags_s,
        )
        if bar is None:
            continue
        bar["symbol"] = symbol
        bars.append(bar)
    return bars


def _query_visible_range(client, symbol: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Aggregate real wall prices over the visible window.

    Keeps strong walls (by notional) **and** price-edge walls so higher
    timeframes still show liquidity further into highs/lows — without inventing
    price levels.
    """
    top = int(TOP_BY_NOTIONAL)
    edge = int(EDGE_BY_PRICE)
    cap = int(MAX_BARS_PER_SIDE)
    # Bids: deep = lowest prices. Asks: deep = highest prices.
    sql_bid = f"""
    WITH agg AS (
      SELECT
        bid_wall_price AS price,
        max(bid_wall_notional) AS value,
        max(bid_wall_qty) AS qty,
        max(mid_price) AS mid,
        max(bid_wall_bps_dist) AS bps_dist,
        max(bucket_start) AS last_ts,
        count() AS samples,
        countIf(positionCaseInsensitive(quality_flags, 'carried_forward') > 0) AS cf_samples,
        anyLast(quality_flags) AS qflags
      FROM {FEATURES_FQN}
      WHERE symbol = {{sym:String}}
        AND bucket_start >= {{start:DateTime64(3, 'UTC')}}
        AND bucket_start < {{end:DateTime64(3, 'UTC')}}
        AND is_valid = 1
        AND bid_wall_price > 0
        AND bid_wall_notional > 0
      GROUP BY bid_wall_price
    ),
    ranked AS (
      SELECT
        *,
        row_number() OVER (ORDER BY value DESC) AS rn_val,
        row_number() OVER (ORDER BY price ASC) AS rn_edge
      FROM agg
    )
    SELECT price, value, qty, mid, bps_dist, last_ts, samples, cf_samples, qflags
    FROM ranked
    WHERE rn_val <= {{top:UInt32}} OR rn_edge <= {{edge:UInt32}}
    ORDER BY value DESC
    LIMIT {{cap:UInt32}}
    """
    sql_ask = f"""
    WITH agg AS (
      SELECT
        ask_wall_price AS price,
        max(ask_wall_notional) AS value,
        max(ask_wall_qty) AS qty,
        max(mid_price) AS mid,
        max(ask_wall_bps_dist) AS bps_dist,
        max(bucket_start) AS last_ts,
        count() AS samples,
        countIf(positionCaseInsensitive(quality_flags, 'carried_forward') > 0) AS cf_samples,
        anyLast(quality_flags) AS qflags
      FROM {FEATURES_FQN}
      WHERE symbol = {{sym:String}}
        AND bucket_start >= {{start:DateTime64(3, 'UTC')}}
        AND bucket_start < {{end:DateTime64(3, 'UTC')}}
        AND is_valid = 1
        AND ask_wall_price > 0
        AND ask_wall_notional > 0
      GROUP BY ask_wall_price
    ),
    ranked AS (
      SELECT
        *,
        row_number() OVER (ORDER BY value DESC) AS rn_val,
        row_number() OVER (ORDER BY price DESC) AS rn_edge
      FROM agg
    )
    SELECT price, value, qty, mid, bps_dist, last_ts, samples, cf_samples, qflags
    FROM ranked
    WHERE rn_val <= {{top:UInt32}} OR rn_edge <= {{edge:UInt32}}
    ORDER BY value DESC
    LIMIT {{cap:UInt32}}
    """
    params = {
        "sym": symbol,
        "start": start,
        "end": end,
        "top": top,
        "edge": edge,
        "cap": cap,
    }
    bid_rows = client.query(sql_bid, parameters=params, settings=_qsettings()).result_rows
    ask_rows = client.query(sql_ask, parameters=params, settings=_qsettings()).result_rows
    bars = _rows_to_bars(symbol, list(bid_rows), side="BID")
    bars.extend(_rows_to_bars(symbol, list(ask_rows), side="ASK"))
    bars.sort(key=lambda b: (0 if b["side"] == "BID" else 1, -(b["value"] or 0)))
    return bars


def _query_snapshot_at(
    client, symbol: str, start: datetime, end: datetime, at: datetime
) -> list[dict[str, Any]]:
    """Causal last bucket with bucket_start <= at, clipped to [start, end)."""
    at_u = _utc(at)
    end_u = _utc(end)
    start_u = _utc(start)
    # Never use future relative to end; never before start.
    causal_end = min(at_u, end_u - timedelta(milliseconds=1))
    if causal_end < start_u:
        return []
    sql = f"""
    SELECT
      bucket_start,
      mid_price,
      bid_wall_price, bid_wall_qty, bid_wall_notional, bid_wall_bps_dist,
      ask_wall_price, ask_wall_qty, ask_wall_notional, ask_wall_bps_dist,
      quality_flags,
      is_valid
    FROM {FEATURES_FQN}
    WHERE symbol = {{sym:String}}
      AND bucket_start >= {{start:DateTime64(3, 'UTC')}}
      AND bucket_start <= {{at:DateTime64(3, 'UTC')}}
      AND bucket_start < {{end:DateTime64(3, 'UTC')}}
      AND is_valid = 1
    ORDER BY bucket_start DESC
    LIMIT 1
    """
    rows = client.query(
        sql,
        parameters={"sym": symbol, "start": start_u, "at": causal_end, "end": end_u},
        settings=_qsettings(),
    ).result_rows
    return _snapshot_to_bars(symbol, rows[0] if rows else None)


def _load_ob200_profile(
    *,
    symbol: str,
    start_u: datetime,
    end_u: datetime,
    at_u: datetime,
    mode_s: str,
    max_bars_per_side: int,
) -> dict[str, Any]:
    notes = [
        "Multi-level walls from local OB200 raw archive (median×3 qty within ~800 bps).",
        "Live tip uses the open hour file (*_open_ob200_v3.zst.tmp) for ~1-minute updates.",
        "History uses closed hour segments; incomplete zstd frames are tolerated.",
    ]
    payload = empty_profile(
        symbol=symbol,
        start=start_u,
        end=end_u,
        mode=mode_s,
        profile_kind="ob200_multi_walls",
        label="OB200 Orderbook Walls",
        source_table=OB200_SOURCE,
        notes=notes,
    )
    try:
        raw = load_ob200_walls(
            symbol=symbol,
            at=at_u,
            max_walls_per_side=max_bars_per_side,
        )
    except Ob200WallsError as exc:
        raise OrderbookProfileQueryError(exc.code, str(exc), status=404) from exc

    mid = raw["mid"] if isinstance(raw["mid"], Decimal) else Decimal(str(raw["mid"]))
    as_of = raw["as_of"]
    bars = _ob200_walls_to_bars(symbol, raw["walls"], as_of=as_of, mid=mid)
    payload["bars"] = bars
    payload["bar_count"] = len(bars)
    payload["bid_count"] = sum(1 for b in bars if b["side"] == "BID")
    payload["ask_count"] = sum(1 for b in bars if b["side"] == "ASK")
    payload["as_of"] = unix_utc(as_of)
    payload["as_of_requested"] = unix_utc(raw["as_of_requested"])
    payload["cached"] = bool(raw.get("cached"))
    payload["ob200"] = {
        "segment": raw.get("segment"),
        "clamped": bool(raw.get("clamped")),
        "live_open": bool(raw.get("live_open")),
        "lag_seconds": raw.get("lag_seconds"),
        "coverage_start": unix_utc(raw["coverage_start"]),
        "coverage_end": unix_utc(raw["coverage_end"]),
        "events_applied": raw.get("events_applied"),
        "bid_levels": raw.get("bid_levels"),
        "ask_levels": raw.get("ask_levels"),
        "mid": float(mid),
    }
    if raw.get("clamped"):
        payload["warning"] = "ob200_clamped_to_coverage_end"
    elif not bars:
        payload["warning"] = "no_wall_data"
    return payload


def load_orderbook_profile(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    at: datetime | None = None,
    mode: str | None = None,
    known_symbol: bool = True,
    max_bars_per_side: int = MAX_BARS_PER_SIDE,
) -> dict[str, Any]:
    """Load current (default) or historical wall bars for the research overlay."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        raise ValueError("invalid_symbol")
    if not known_symbol and sym != "XAUUSDT":
        raise KeyError(sym)
    start_u = _utc(start)
    end_u = _utc(end)
    if end_u <= start_u:
        raise ValueError("invalid_time_range")
    span = (end_u - start_u).total_seconds()
    if span > MAX_RANGE_SECONDS:
        raise ValueError("time_range_too_large")

    mode_s = str(mode or "").strip().lower()
    if mode_s == "visible_range":
        mode_s = "history"
    if mode_s in {"current", ""}:
        mode_s = "snapshot_at"
    if mode_s not in {"snapshot_at", "history", "ob200", "features"}:
        mode_s = "snapshot_at"

    at_u = _utc(at) if at is not None else (end_u - timedelta(milliseconds=1))
    if mode_s in {"snapshot_at", "ob200", "features"} and at_u >= end_u:
        at_u = end_u - timedelta(milliseconds=1)

    prefer_ob200 = mode_s == "ob200" or (
        mode_s == "snapshot_at" and has_ob200_archive(sym)
    )
    source_tag = "ob200" if prefer_ob200 else mode_s

    cache_key = (
        sym,
        unix_utc(start_u),
        unix_utc(end_u),
        unix_utc(at_u),
        source_tag,
        int(max_bars_per_side),
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    if prefer_ob200:
        try:
            payload = _load_ob200_profile(
                symbol=sym,
                start_u=start_u,
                end_u=end_u,
                at_u=at_u,
                mode_s="ob200" if mode_s == "ob200" else "snapshot_at",
                max_bars_per_side=max_bars_per_side,
            )
            now = datetime.now(timezone.utc)
            ttl = _LIVE_TTL if (now - end_u).total_seconds() < 120 else _HIST_TTL
            _cache_put(cache_key, payload, ttl)
            return payload
        except OrderbookProfileQueryError:
            if mode_s == "ob200":
                raise
            # Fall through to features if OB200 missing for this as-of.

    payload = empty_profile(symbol=sym, start=start_u, end=end_u, mode=mode_s)
    try:
        client = _client()
        if mode_s == "history":
            bars = _query_visible_range(client, sym, start_u, end_u)
        else:
            bars = _query_snapshot_at(client, sym, start_u, end_u, at_u)
    except (DatabaseError, OperationalError) as exc:
        raise _translate_ch(exc) from exc
    except OrderbookProfileQueryError:
        raise
    except Exception as exc:
        raise _translate_ch(exc) from exc

    payload["bars"] = bars
    payload["bar_count"] = len(bars)
    payload["bid_count"] = sum(1 for b in bars if b["side"] == "BID")
    payload["ask_count"] = sum(1 for b in bars if b["side"] == "ASK")
    payload["as_of"] = unix_utc(at_u) if mode_s != "history" else None
    if not bars:
        payload["warning"] = "no_wall_data"

    now = datetime.now(timezone.utc)
    ttl = _LIVE_TTL if (now - end_u).total_seconds() < 120 else _HIST_TTL
    _cache_put(cache_key, payload, ttl)
    return payload

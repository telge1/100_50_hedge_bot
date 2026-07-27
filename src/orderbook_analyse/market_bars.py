"""Phase 3: full-history market context bars (price, tradeflow, OI, liquidations).

Read-only ClickHouse aggregations. No walls, patterns, or trading signals.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Protocol, Sequence

from orderbook_analyse.dynamic_wall_detector import _ensure_aware

logger = logging.getLogger(__name__)

SUPPORTED_TIMEFRAMES: dict[str, int] = {"1m": 60, "5m": 300}
DEFAULT_BAR_TIMEFRAMES = ("1m", "5m")
DEFAULT_TINY_LIQUIDATION_NOTIONAL = Decimal("1.0")
DEFAULT_OI_FLAT_TOL_PCT = 1e-8
DEFAULT_PRICE_FLAT_TOL_PCT = 1e-8
DEFAULT_MAX_BAR_RANGE_PCT = 20.0
DEFAULT_MAX_OI_OPEN_CLOSE_RATIO = 100.0
DEFAULT_VWAP_ABS_EPSILON = 1e-12
DEFAULT_VWAP_REL_EPSILON = 1e-12

PHASE3_OUTPUT_BASE = ("price_summary.csv", "liquidations.csv")


class SupportsQuery(Protocol):
    def query(self, sql: str, parameters: Mapping[str, Any] | None = None) -> Any: ...


class MarketContextError(RuntimeError):
    pass


def parse_bar_timeframes(raw: str | Sequence[str] | None) -> list[str]:
    if raw is None or raw == "":
        return list(DEFAULT_BAR_TIMEFRAMES)
    if isinstance(raw, str):
        parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    else:
        parts = [str(p).strip().lower() for p in raw if str(p).strip()]
    if not parts:
        raise MarketContextError("bar-timeframes must not be empty")
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        if p not in SUPPORTED_TIMEFRAMES:
            raise MarketContextError(
                f"unsupported bar timeframe {p!r}; supported: {sorted(SUPPORTED_TIMEFRAMES)}"
            )
        if p not in seen:
            out.append(p)
            seen.add(p)
    return out


def timeframe_seconds(tf: str) -> int:
    if tf not in SUPPORTED_TIMEFRAMES:
        raise MarketContextError(f"unsupported timeframe {tf!r}")
    return SUPPORTED_TIMEFRAMES[tf]


def _ch_interval(tf: str) -> str:
    sec = timeframe_seconds(tf)
    if sec % 60 == 0:
        return f"INTERVAL {sec // 60} MINUTE"
    return f"INTERVAL {sec} SECOND"


def _to_iso(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return _ensure_aware(v).isoformat()
    return str(v)


def _dec(v: Any) -> Decimal | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _fmt_dec(v: Any) -> str | None:
    d = _dec(v)
    return None if d is None else format(d, "f")


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _pct_change(start: Decimal | None, end: Decimal | None) -> float | None:
    if start is None or end is None or start == 0:
        return None
    return _safe_float((end - start) / start * Decimal("100"))


def _ratio(num: Decimal | None, den: Decimal | None) -> float | None:
    if num is None or den is None or den == 0:
        return None
    return _safe_float(num / den)


def spread_and_bps(bid: Decimal | None, ask: Decimal | None) -> tuple[Decimal | None, float | None]:
    if bid is None or ask is None:
        return None, None
    mid = (bid + ask) / Decimal("2")
    if mid <= 0:
        return None, None
    spread = ask - bid
    return spread, _safe_float(spread / mid * Decimal("10000"))


def classify_oi_quadrant(
    price_change_pct: float | None,
    oi_change_pct: float | None,
    *,
    price_tol: float = DEFAULT_PRICE_FLAT_TOL_PCT,
    oi_tol: float = DEFAULT_OI_FLAT_TOL_PCT,
) -> str:
    if price_change_pct is None and oi_change_pct is None:
        return "UNKNOWN"
    if price_change_pct is None:
        return "UNKNOWN"
    if abs(price_change_pct) <= price_tol:
        return "PRICE_FLAT"
    if oi_change_pct is None:
        return "UNKNOWN"
    if abs(oi_change_pct) <= oi_tol:
        return "OI_FLAT"
    price_up = price_change_pct > 0
    oi_up = oi_change_pct > 0
    if price_up and oi_up:
        return "PRICE_UP_OI_UP"
    if price_up and not oi_up:
        return "PRICE_UP_OI_DOWN"
    if (not price_up) and oi_up:
        return "PRICE_DOWN_OI_UP"
    return "PRICE_DOWN_OI_DOWN"


def compute_max_drawdown_runup(closes: Sequence[float | Decimal | None]) -> tuple[float | None, float | None]:
    peak: float | None = None
    trough: float | None = None
    max_dd: float | None = None
    max_ru: float | None = None
    for c in closes:
        if c is None:
            continue
        px = float(c)
        if peak is None or px > peak:
            peak = px
        if trough is None or px < trough:
            trough = px
        if peak is not None and peak > 0:
            dd = (px - peak) / peak * 100.0
            max_dd = dd if max_dd is None else min(max_dd, dd)
        if trough is not None and trough > 0:
            ru = (px - trough) / trough * 100.0
            max_ru = ru if max_ru is None else max(max_ru, ru)
    return max_dd, max_ru


def _rows_from_query(result: Any) -> list[dict[str, Any]]:
    cols = list(getattr(result, "column_names", []) or [])
    return [dict(zip(cols, row, strict=True)) for row in getattr(result, "result_rows", []) or []]


def query_price_bars(db: SupportsQuery, *, symbol: str, start: datetime, end: datetime, timeframe: str) -> list[dict[str, Any]]:
    """Aggregate ticker OHLC/OI bars for one symbol.

    ClickHouse resolves bare ``symbol`` in WHERE to a SELECT alias when the
    query also projects ``… AS symbol``. Qualify the table column so the filter
    cannot be shadowed (otherwise APT+BTC rows share one bucket).
    """
    interval = _ch_interval(timeframe)
    seconds = timeframe_seconds(timeframe)
    sql = f"""
        SELECT %(symbol)s AS symbol,
            toStartOfInterval(t.exchange_ts, {interval}) AS bucket_start,
            toStartOfInterval(t.exchange_ts, {interval}) + toIntervalSecond(%(seconds)s) AS bucket_end,
            count() AS sample_count,
            argMin(t.last_price, t.exchange_ts) AS open_price,
            max(t.last_price) AS high_price,
            min(t.last_price) AS low_price,
            argMax(t.last_price, t.exchange_ts) AS close_price,
            argMin(t.mark_price, t.exchange_ts) AS mark_open,
            argMax(t.mark_price, t.exchange_ts) AS mark_close,
            argMin(t.index_price, t.exchange_ts) AS index_open,
            argMax(t.index_price, t.exchange_ts) AS index_close,
            argMin(t.best_bid_price, t.exchange_ts) AS best_bid_open,
            argMax(t.best_bid_price, t.exchange_ts) AS best_bid_close,
            argMin(t.best_ask_price, t.exchange_ts) AS best_ask_open,
            argMax(t.best_ask_price, t.exchange_ts) AS best_ask_close,
            argMin(t.open_interest, t.exchange_ts) AS open_interest_open,
            argMax(t.open_interest, t.exchange_ts) AS open_interest_close,
            argMin(t.open_interest_value, t.exchange_ts) AS open_interest_value_open,
            argMax(t.open_interest_value, t.exchange_ts) AS open_interest_value_close,
            argMin(t.funding_rate, t.exchange_ts) AS funding_rate_open,
            argMax(t.funding_rate, t.exchange_ts) AS funding_rate_close,
            argMin(t.volume_24h, t.exchange_ts) AS volume_24h_open,
            argMax(t.volume_24h, t.exchange_ts) AS volume_24h_close,
            argMin(t.turnover_24h, t.exchange_ts) AS turnover_24h_open,
            argMax(t.turnover_24h, t.exchange_ts) AS turnover_24h_close
        FROM ticker_samples AS t
        WHERE t.symbol = %(symbol)s AND t.exchange_ts >= %(start)s AND t.exchange_ts <= %(end)s
        GROUP BY bucket_start, bucket_end
        ORDER BY bucket_start
    """
    return [enrich_price_bar(r) for r in _rows_from_query(db.query(sql, parameters={"symbol": symbol, "start": start, "end": end, "seconds": seconds}))]


def enrich_price_bar(row: Mapping[str, Any]) -> dict[str, Any]:
    open_p, high_p, low_p, close_p = _dec(row.get("open_price")), _dec(row.get("high_price")), _dec(row.get("low_price")), _dec(row.get("close_price"))
    bid_o, ask_o = _dec(row.get("best_bid_open")), _dec(row.get("best_ask_open"))
    bid_c, ask_c = _dec(row.get("best_bid_close")), _dec(row.get("best_ask_close"))
    spread_o, bps_o = spread_and_bps(bid_o, ask_o)
    spread_c, bps_c = spread_and_bps(bid_c, ask_c)
    oi_o, oi_c = _dec(row.get("open_interest_open")), _dec(row.get("open_interest_close"))
    return {
        "symbol": row.get("symbol"), "bucket_start": _to_iso(row.get("bucket_start")), "bucket_end": _to_iso(row.get("bucket_end")),
        "sample_count": int(row.get("sample_count") or 0),
        "open_price": _fmt_dec(open_p), "high_price": _fmt_dec(high_p), "low_price": _fmt_dec(low_p), "close_price": _fmt_dec(close_p),
        "mark_open": _fmt_dec(row.get("mark_open")), "mark_close": _fmt_dec(row.get("mark_close")),
        "index_open": _fmt_dec(row.get("index_open")), "index_close": _fmt_dec(row.get("index_close")),
        "best_bid_open": _fmt_dec(bid_o), "best_bid_close": _fmt_dec(bid_c), "best_ask_open": _fmt_dec(ask_o), "best_ask_close": _fmt_dec(ask_c),
        "spread_open": _fmt_dec(spread_o), "spread_close": _fmt_dec(spread_c), "spread_bps_open": bps_o, "spread_bps_close": bps_c,
        "price_change_pct": _pct_change(open_p, close_p), "high_from_open_pct": _pct_change(open_p, high_p), "low_from_open_pct": _pct_change(open_p, low_p),
        "range_pct": None if open_p is None or high_p is None or low_p is None or open_p == 0 else _safe_float((high_p - low_p) / open_p * Decimal("100")),
        "open_interest_open": _fmt_dec(oi_o), "open_interest_close": _fmt_dec(oi_c),
        "open_interest_change_abs": _fmt_dec(None if oi_o is None or oi_c is None else oi_c - oi_o), "open_interest_change_pct": _pct_change(oi_o, oi_c),
        "open_interest_value_open": _fmt_dec(row.get("open_interest_value_open")), "open_interest_value_close": _fmt_dec(row.get("open_interest_value_close")),
        "funding_rate_open": _fmt_dec(row.get("funding_rate_open")), "funding_rate_close": _fmt_dec(row.get("funding_rate_close")),
        "volume_24h_open": _fmt_dec(row.get("volume_24h_open")), "volume_24h_close": _fmt_dec(row.get("volume_24h_close")),
        "turnover_24h_open": _fmt_dec(row.get("turnover_24h_open")), "turnover_24h_close": _fmt_dec(row.get("turnover_24h_close")),
    }


PRICE_BAR_HEADERS = [
    "symbol", "bucket_start", "bucket_end", "sample_count", "open_price", "high_price", "low_price", "close_price",
    "mark_open", "mark_close", "index_open", "index_close", "best_bid_open", "best_bid_close", "best_ask_open", "best_ask_close",
    "spread_open", "spread_close", "spread_bps_open", "spread_bps_close", "price_change_pct", "high_from_open_pct", "low_from_open_pct", "range_pct",
    "open_interest_open", "open_interest_close", "open_interest_change_abs", "open_interest_change_pct",
    "open_interest_value_open", "open_interest_value_close", "funding_rate_open", "funding_rate_close",
    "volume_24h_open", "volume_24h_close", "turnover_24h_open", "turnover_24h_close",
]


def query_tradeflow_bars(db: SupportsQuery, *, symbol: str, start: datetime, end: datetime, timeframe: str) -> list[dict[str, Any]]:
    """Aggregate public trades for one symbol (table-qualified symbol filter)."""
    interval = _ch_interval(timeframe)
    seconds = timeframe_seconds(timeframe)
    sql = f"""
        SELECT %(symbol)s AS symbol,
            toStartOfInterval(t.trade_ts, {interval}) AS bucket_start,
            toStartOfInterval(t.trade_ts, {interval}) + toIntervalSecond(%(seconds)s) AS bucket_end,
            count() AS trade_count, countIf(t.side = 'Buy') AS buy_trade_count, countIf(t.side = 'Sell') AS sell_trade_count,
            sum(t.quantity) AS total_quantity, sumIf(t.quantity, t.side = 'Buy') AS buy_quantity, sumIf(t.quantity, t.side = 'Sell') AS sell_quantity,
            sum(t.notional) AS total_notional, sumIf(t.notional, t.side = 'Buy') AS buy_notional, sumIf(t.notional, t.side = 'Sell') AS sell_notional,
            max(t.notional) AS largest_trade_notional, maxIf(t.notional, t.side = 'Buy') AS largest_buy_notional,
            maxIf(t.notional, t.side = 'Sell') AS largest_sell_notional, countIf(t.is_block_trade) AS block_trade_count,
            countIf(t.is_rpi_trade) AS rpi_trade_count,
            argMin(t.price, t.trade_ts) AS first_trade_price, argMax(t.price, t.trade_ts) AS last_trade_price,
            min(t.price) AS min_trade_price, max(t.price) AS max_trade_price
        FROM public_trades AS t
        WHERE t.symbol = %(symbol)s AND t.trade_ts >= %(start)s AND t.trade_ts <= %(end)s
        GROUP BY bucket_start, bucket_end ORDER BY bucket_start
    """
    return [enrich_tradeflow_bar(r) for r in _rows_from_query(db.query(sql, parameters={"symbol": symbol, "start": start, "end": end, "seconds": seconds}))]


def enrich_tradeflow_bar(row: Mapping[str, Any]) -> dict[str, Any]:
    buy_n = _dec(row.get("buy_notional")) or Decimal("0")
    sell_n = _dec(row.get("sell_notional")) or Decimal("0")
    total_n = _dec(row.get("total_notional")) or Decimal("0")
    buy_q = _dec(row.get("buy_quantity")) or Decimal("0")
    sell_q = _dec(row.get("sell_quantity")) or Decimal("0")
    total_q = _dec(row.get("total_quantity")) or Decimal("0")
    first_p, last_p = _dec(row.get("first_trade_price")), _dec(row.get("last_trade_price"))
    min_p, max_p = _dec(row.get("min_trade_price")), _dec(row.get("max_trade_price"))
    vwap = _safe_float(float(total_n) / float(total_q)) if total_q != 0 else None
    return {
        "symbol": row.get("symbol"), "bucket_start": _to_iso(row.get("bucket_start")), "bucket_end": _to_iso(row.get("bucket_end")),
        "trade_count": int(row.get("trade_count") or 0), "buy_trade_count": int(row.get("buy_trade_count") or 0), "sell_trade_count": int(row.get("sell_trade_count") or 0),
        "total_quantity": _fmt_dec(total_q), "buy_quantity": _fmt_dec(buy_q), "sell_quantity": _fmt_dec(sell_q),
        "total_notional": _fmt_dec(total_n), "buy_notional": _fmt_dec(buy_n), "sell_notional": _fmt_dec(sell_n),
        "delta_quantity": _fmt_dec(buy_q - sell_q), "delta_notional": _fmt_dec(buy_n - sell_n),
        "delta_ratio": _ratio(buy_n - sell_n, total_n), "buy_share": _ratio(buy_n, total_n), "sell_share": _ratio(sell_n, total_n),
        "vwap": vwap, "largest_trade_notional": _fmt_dec(row.get("largest_trade_notional")),
        "largest_buy_notional": _fmt_dec(row.get("largest_buy_notional")), "largest_sell_notional": _fmt_dec(row.get("largest_sell_notional")),
        "block_trade_count": int(row.get("block_trade_count") or 0), "rpi_trade_count": int(row.get("rpi_trade_count") or 0),
        "first_trade_price": _fmt_dec(first_p), "last_trade_price": _fmt_dec(last_p),
        "min_trade_price": _fmt_dec(min_p), "max_trade_price": _fmt_dec(max_p),
        "trade_price_change_pct": _pct_change(first_p, last_p),
    }


TRADEFLOW_HEADERS = [
    "symbol", "bucket_start", "bucket_end", "trade_count", "buy_trade_count", "sell_trade_count",
    "total_quantity", "buy_quantity", "sell_quantity", "total_notional", "buy_notional", "sell_notional",
    "delta_quantity", "delta_notional", "delta_ratio", "buy_share", "sell_share", "vwap",
    "largest_trade_notional", "largest_buy_notional", "largest_sell_notional", "block_trade_count", "rpi_trade_count",
    "first_trade_price", "last_trade_price", "min_trade_price", "max_trade_price", "trade_price_change_pct",
]


def oi_bars_from_price_bars(price_bars: Sequence[Mapping[str, Any]], *, price_tol: float = DEFAULT_PRICE_FLAT_TOL_PCT, oi_tol: float = DEFAULT_OI_FLAT_TOL_PCT) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in price_bars:
        oi_o, oi_c = _dec(p.get("open_interest_open")), _dec(p.get("open_interest_close"))
        px_o, px_c = _dec(p.get("open_price")), _dec(p.get("close_price"))
        oi_chg, px_chg = _pct_change(oi_o, oi_c), _pct_change(px_o, px_c)
        oi_vals = [v for v in (oi_o, oi_c) if v is not None]
        rows.append({
            "symbol": p.get("symbol"), "bucket_start": p.get("bucket_start"), "bucket_end": p.get("bucket_end"), "sample_count": p.get("sample_count"),
            "oi_open": _fmt_dec(oi_o), "oi_high": _fmt_dec(max(oi_vals) if oi_vals else None), "oi_low": _fmt_dec(min(oi_vals) if oi_vals else None), "oi_close": _fmt_dec(oi_c),
            "oi_change_abs": p.get("open_interest_change_abs"), "oi_change_pct": oi_chg,
            "oi_value_open": p.get("open_interest_value_open"), "oi_value_close": p.get("open_interest_value_close"),
            "price_open": p.get("open_price"), "price_close": p.get("close_price"), "price_change_pct": px_chg,
            "context_quadrant": classify_oi_quadrant(px_chg, oi_chg, price_tol=price_tol, oi_tol=oi_tol),
        })
    return rows


OI_HEADERS = [
    "symbol", "bucket_start", "bucket_end", "sample_count", "oi_open", "oi_high", "oi_low", "oi_close",
    "oi_change_abs", "oi_change_pct", "oi_value_open", "oi_value_close", "price_open", "price_close", "price_change_pct", "context_quadrant",
]


def _parse_ts(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return _ensure_aware(v)
    try:
        return _ensure_aware(datetime.fromisoformat(str(v).replace("Z", "+00:00")))
    except ValueError:
        return None


def query_liquidations(db: SupportsQuery, *, symbol: str, start: datetime, end: datetime, tiny_notional: Decimal = DEFAULT_TINY_LIQUIDATION_NOTIONAL) -> list[dict[str, Any]]:
    """Load liquidations in half-open window ``[start, end)``.

    ClickHouse DateTime64(3) query params may truncate to whole seconds, which
    drops sub-second events when ``start == end`` (single-row clip) or when the
    event sits in the final millisecond of ``end``. We query with ``end + 1s``
    and re-filter in Python on ``liquidation_ts``.
    """
    start_u = _ensure_aware(start)
    end_u = _ensure_aware(end)
    query_end = end_u + timedelta(seconds=1)
    sql = """
        SELECT t.symbol, t.liquidation_ts, t.side, t.price, t.quantity, t.notional
        FROM liquidations AS t
        WHERE t.symbol = %(symbol)s
          AND t.liquidation_ts >= %(start)s
          AND t.liquidation_ts < %(query_end)s
        ORDER BY t.liquidation_ts, t.side, t.price
    """
    out: list[dict[str, Any]] = []
    for r in _rows_from_query(
        db.query(sql, parameters={"symbol": symbol, "start": start_u, "query_end": query_end})
    ):
        ts = _parse_ts(r.get("liquidation_ts"))
        if ts is None or ts < start_u or ts >= end_u:
            continue
        n = _dec(r.get("notional")) or Decimal("0")
        out.append({
            "symbol": r.get("symbol") or symbol, "liquidation_ts": _to_iso(ts), "side": r.get("side"),
            "price": _fmt_dec(r.get("price")), "quantity": _fmt_dec(r.get("quantity")), "notional": _fmt_dec(n),
            "is_tiny_event": bool(n < tiny_notional), "price_at_or_before_event": None, "price_change_1m_after": None,
        })
    return out


LIQUIDATION_EVENT_HEADERS = [
    "symbol", "liquidation_ts", "side", "price", "quantity", "notional", "is_tiny_event", "price_at_or_before_event", "price_change_1m_after",
]


def query_liquidation_bars(db: SupportsQuery, *, symbol: str, start: datetime, end: datetime, timeframe: str, tiny_notional: Decimal = DEFAULT_TINY_LIQUIDATION_NOTIONAL) -> list[dict[str, Any]]:
    """Aggregate liquidations in half-open window ``[start, end)`` (same pad as events)."""
    events = query_liquidations(db, symbol=symbol, start=start, end=end, tiny_notional=tiny_notional)
    if not events:
        return []
    seconds = timeframe_seconds(timeframe)
    buckets: dict[datetime, dict[str, Any]] = {}
    for e in events:
        ts = _parse_ts(e["liquidation_ts"])
        if ts is None:
            continue
        ts_u = _ensure_aware(ts)
        floor_epoch = int(ts_u.timestamp()) // seconds * seconds
        bucket_start = datetime.fromtimestamp(floor_epoch, tz=timezone.utc)
        bucket_end = bucket_start + timedelta(seconds=seconds)
        b = buckets.get(bucket_start)
        if b is None:
            b = {
                "symbol": symbol,
                "bucket_start": bucket_start,
                "bucket_end": bucket_end,
                "event_count": 0,
                "buy_event_count": 0,
                "sell_event_count": 0,
                "buy_quantity": Decimal("0"),
                "sell_quantity": Decimal("0"),
                "buy_notional": Decimal("0"),
                "sell_notional": Decimal("0"),
                "total_notional": Decimal("0"),
                "largest_event_notional": Decimal("0"),
                "tiny_event_count": 0,
            }
            buckets[bucket_start] = b
        n = _dec(e.get("notional")) or Decimal("0")
        q = _dec(e.get("quantity")) or Decimal("0")
        side = e.get("side")
        b["event_count"] += 1
        b["total_notional"] += n
        if n > b["largest_event_notional"]:
            b["largest_event_notional"] = n
        if e.get("is_tiny_event"):
            b["tiny_event_count"] += 1
        if side == "Buy":
            b["buy_event_count"] += 1
            b["buy_quantity"] += q
            b["buy_notional"] += n
        elif side == "Sell":
            b["sell_event_count"] += 1
            b["sell_quantity"] += q
            b["sell_notional"] += n
    out: list[dict[str, Any]] = []
    for bs in sorted(buckets):
        b = buckets[bs]
        out.append({
            "symbol": b["symbol"],
            "bucket_start": _to_iso(b["bucket_start"]),
            "bucket_end": _to_iso(b["bucket_end"]),
            "event_count": b["event_count"],
            "buy_event_count": b["buy_event_count"],
            "sell_event_count": b["sell_event_count"],
            "buy_quantity": _fmt_dec(b["buy_quantity"]),
            "sell_quantity": _fmt_dec(b["sell_quantity"]),
            "buy_notional": _fmt_dec(b["buy_notional"]),
            "sell_notional": _fmt_dec(b["sell_notional"]),
            "total_notional": _fmt_dec(b["total_notional"]),
            "largest_event_notional": _fmt_dec(b["largest_event_notional"]),
            "tiny_event_count": b["tiny_event_count"],
        })
    return out


LIQUIDATION_BAR_HEADERS = [
    "symbol", "bucket_start", "bucket_end", "event_count", "buy_event_count", "sell_event_count",
    "buy_quantity", "sell_quantity", "buy_notional", "sell_notional", "total_notional", "largest_event_notional", "tiny_event_count",
]


def build_timeline_rows(*, price_bars: Sequence[Mapping[str, Any]], tradeflow_bars: Sequence[Mapping[str, Any]], oi_bars: Sequence[Mapping[str, Any]], liquidation_bars: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    tf_by = {r["bucket_start"]: r for r in tradeflow_bars}
    oi_by = {r["bucket_start"]: r for r in oi_bars}
    liq_by = {r["bucket_start"]: r for r in liquidation_bars}
    out: list[dict[str, Any]] = []
    for p in price_bars:
        bs = p.get("bucket_start")
        tf, oi, liq = tf_by.get(bs) or {}, oi_by.get(bs) or {}, liq_by.get(bs) or {}
        sources = ["ticker"]
        if tf:
            sources.append("trades")
        if oi.get("oi_open") is not None or oi.get("oi_close") is not None:
            sources.append("oi")
        if liq:
            sources.append("liquidations")
        out.append({
            "symbol": p.get("symbol"), "bucket_start": bs, "bucket_end": p.get("bucket_end"),
            "open_price": p.get("open_price"), "high_price": p.get("high_price"), "low_price": p.get("low_price"), "close_price": p.get("close_price"),
            "price_change_pct": p.get("price_change_pct"), "range_pct": p.get("range_pct"), "spread_bps_close": p.get("spread_bps_close"),
            "trade_count": tf.get("trade_count"), "total_notional": tf.get("total_notional"), "buy_notional": tf.get("buy_notional"), "sell_notional": tf.get("sell_notional"),
            "delta_notional": tf.get("delta_notional"), "delta_ratio": tf.get("delta_ratio"), "vwap": tf.get("vwap"),
            "oi_open": oi.get("oi_open"), "oi_close": oi.get("oi_close"), "oi_change_pct": oi.get("oi_change_pct"), "context_quadrant": oi.get("context_quadrant"),
            "liquidation_count": liq.get("event_count"), "liquidation_notional": liq.get("total_notional"),
            "buy_liquidation_notional": liq.get("buy_notional"), "sell_liquidation_notional": liq.get("sell_notional"),
            "data_sources_present": "|".join(sources),
        })
    return out


TIMELINE_HEADERS = [
    "symbol", "bucket_start", "bucket_end", "open_price", "high_price", "low_price", "close_price", "price_change_pct", "range_pct", "spread_bps_close",
    "trade_count", "total_notional", "buy_notional", "sell_notional", "delta_notional", "delta_ratio", "vwap",
    "oi_open", "oi_close", "oi_change_pct", "context_quadrant", "liquidation_count", "liquidation_notional",
    "buy_liquidation_notional", "sell_liquidation_notional", "data_sources_present",
]


def query_trade_vwap_window(db: SupportsQuery, *, symbol: str, start: datetime, end: datetime) -> dict[str, Any]:
    sql = """
        SELECT count() AS trade_count, sum(t.notional) AS total_notional,
            sumIf(t.notional, t.side = 'Buy') AS buy_notional, sumIf(t.notional, t.side = 'Sell') AS sell_notional,
            sum(t.quantity) AS total_quantity
        FROM public_trades AS t
        WHERE t.symbol = %(symbol)s AND t.trade_ts >= %(start)s AND t.trade_ts <= %(end)s
    """
    rows = _rows_from_query(db.query(sql, parameters={"symbol": symbol, "start": start, "end": end}))
    if not rows:
        return {"trade_count": 0, "total_notional": None, "buy_notional": None, "sell_notional": None, "vwap": None}
    r = rows[0]
    total_n, total_q = _dec(r.get("total_notional")), _dec(r.get("total_quantity"))
    vwap = _safe_float(float(total_n) / float(total_q)) if total_n is not None and total_q is not None and total_q != 0 else None
    return {"trade_count": int(r.get("trade_count") or 0), "total_notional": _fmt_dec(total_n), "buy_notional": _fmt_dec(r.get("buy_notional")), "sell_notional": _fmt_dec(r.get("sell_notional")), "vwap": vwap}


def query_ticker_window_stats(db: SupportsQuery, *, symbol: str, start: datetime, end: datetime) -> dict[str, Any]:
    sql = """
        SELECT min(t.exchange_ts) AS first_ts, max(t.exchange_ts) AS last_ts, count() AS sample_count,
            argMin(t.last_price, t.exchange_ts) AS start_price, argMax(t.last_price, t.exchange_ts) AS end_price,
            max(t.last_price) AS high_price, argMax(t.exchange_ts, t.last_price) AS high_ts,
            min(t.last_price) AS low_price, argMin(t.exchange_ts, t.last_price) AS low_ts,
            argMin(t.open_interest, t.exchange_ts) AS oi_start, argMax(t.open_interest, t.exchange_ts) AS oi_end
        FROM ticker_samples AS t
        WHERE t.symbol = %(symbol)s AND t.exchange_ts >= %(start)s AND t.exchange_ts <= %(end)s
    """
    rows = _rows_from_query(db.query(sql, parameters={"symbol": symbol, "start": start, "end": end}))
    if not rows or int(rows[0].get("sample_count") or 0) == 0:
        return {"sample_count": 0}
    r = rows[0]
    start_p, end_p, high_p, low_p = _dec(r.get("start_price")), _dec(r.get("end_price")), _dec(r.get("high_price")), _dec(r.get("low_price"))
    oi_s, oi_e = _dec(r.get("oi_start")), _dec(r.get("oi_end"))
    return {
        "first_ts": _to_iso(r.get("first_ts")), "last_ts": _to_iso(r.get("last_ts")), "sample_count": int(r.get("sample_count") or 0),
        "start_price": _fmt_dec(start_p), "end_price": _fmt_dec(end_p), "high_price": _fmt_dec(high_p), "high_ts": _to_iso(r.get("high_ts")),
        "low_price": _fmt_dec(low_p), "low_ts": _to_iso(r.get("low_ts")),
        "net_change_pct": _pct_change(start_p, end_p), "high_to_low_pct": _pct_change(high_p, low_p),
        "full_range_pct": None if start_p is None or high_p is None or low_p is None or start_p == 0 else _safe_float((high_p - low_p) / start_p * Decimal("100")),
        "oi_start": _fmt_dec(oi_s), "oi_end": _fmt_dec(oi_e), "oi_change_pct": _pct_change(oi_s, oi_e),
    }


def build_price_summary(*, symbol: str, ticker_stats: Mapping[str, Any], price_bars_by_tf: Mapping[str, Sequence[Mapping[str, Any]]], trade_stats: Mapping[str, Any]) -> dict[str, Any]:
    spreads, spread_bps, closes = [], [], []
    bars_1m, bars_5m = list(price_bars_by_tf.get("1m") or []), list(price_bars_by_tf.get("5m") or [])
    for b in bars_1m or bars_5m:
        so, sb, cp = _safe_float(b.get("spread_close")), _safe_float(b.get("spread_bps_close")), _safe_float(b.get("close_price"))
        if so is not None:
            spreads.append(so)
        if sb is not None:
            spread_bps.append(sb)
        if cp is not None:
            closes.append(cp)

    def _ext(bars: Sequence[Mapping[str, Any]], positive: bool) -> float | None:
        vals = [v for v in (_safe_float(b.get("price_change_pct")) for b in bars) if v is not None]
        return (max(vals) if positive else min(vals)) if vals else None

    dd, ru = compute_max_drawdown_runup(closes)
    return {
        "symbol": symbol, "first_ts": ticker_stats.get("first_ts"), "last_ts": ticker_stats.get("last_ts"), "sample_count": ticker_stats.get("sample_count") or 0,
        "start_price": ticker_stats.get("start_price"), "end_price": ticker_stats.get("end_price"), "high_price": ticker_stats.get("high_price"), "high_ts": ticker_stats.get("high_ts"),
        "low_price": ticker_stats.get("low_price"), "low_ts": ticker_stats.get("low_ts"),
        "net_change_pct": ticker_stats.get("net_change_pct"), "high_to_low_pct": ticker_stats.get("high_to_low_pct"), "full_range_pct": ticker_stats.get("full_range_pct"),
        "vwap": trade_stats.get("vwap"),
        "average_spread": sum(spreads) / len(spreads) if spreads else None,
        "median_spread": sorted(spreads)[len(spreads) // 2] if spreads else None,
        "average_spread_bps": sum(spread_bps) / len(spread_bps) if spread_bps else None,
        "median_spread_bps": sorted(spread_bps)[len(spread_bps) // 2] if spread_bps else None,
        "largest_positive_1m_bar_pct": _ext(bars_1m, True), "largest_negative_1m_bar_pct": _ext(bars_1m, False),
        "largest_positive_5m_bar_pct": _ext(bars_5m, True), "largest_negative_5m_bar_pct": _ext(bars_5m, False),
        "maximum_drawdown_pct": dd, "maximum_runup_pct": ru,
    }


PRICE_SUMMARY_HEADERS = [
    "symbol", "first_ts", "last_ts", "sample_count", "start_price", "end_price", "high_price", "high_ts", "low_price", "low_ts",
    "net_change_pct", "high_to_low_pct", "full_range_pct", "vwap", "average_spread", "median_spread", "average_spread_bps", "median_spread_bps",
    "largest_positive_1m_bar_pct", "largest_negative_1m_bar_pct", "largest_positive_5m_bar_pct", "largest_negative_5m_bar_pct",
    "maximum_drawdown_pct", "maximum_runup_pct",
]


@dataclass
class MarketContextResult:
    timeframes: list[str]
    price_bars: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    tradeflow_bars: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    oi_bars: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    liquidation_bars: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    timelines: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    liquidations: list[dict[str, Any]] = field(default_factory=list)
    price_summary: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    ok: bool = True
    error_message: str | None = None


def _clip_table(table_ranges: Mapping[str, Mapping[str, Any]], table: str, analysis_start: datetime, analysis_end: datetime) -> tuple[datetime | None, datetime | None]:
    info = table_ranges.get(table) or {}
    first, last = info.get("first_ts"), info.get("last_ts")
    if first is None or last is None:
        return None, None
    lo = max(_ensure_aware(first), _ensure_aware(analysis_start))
    hi = min(_ensure_aware(last), _ensure_aware(analysis_end))
    return (None, None) if lo > hi else (lo, hi)


def run_market_context(
    db: SupportsQuery,
    *,
    symbol: str,
    analysis_start: datetime | None,
    analysis_end: datetime | None,
    table_ranges: Mapping[str, Mapping[str, Any]],
    timeframes: Sequence[str],
    tiny_liquidation_notional: Decimal = DEFAULT_TINY_LIQUIDATION_NOTIONAL,
    max_bar_range_pct: float = DEFAULT_MAX_BAR_RANGE_PCT,
    max_oi_open_close_ratio: float = DEFAULT_MAX_OI_OPEN_CLOSE_RATIO,
) -> MarketContextResult:
    tfs = parse_bar_timeframes(list(timeframes))
    result = MarketContextResult(timeframes=tfs)
    if analysis_start is None or analysis_end is None:
        result.ok = False
        result.error_message = "no analysis window for market context"
        return result
    a_start, a_end = _ensure_aware(analysis_start), _ensure_aware(analysis_end)
    ticker_lo, ticker_hi = _clip_table(table_ranges, "ticker_samples", a_start, a_end)
    trade_lo, trade_hi = _clip_table(table_ranges, "public_trades", a_start, a_end)
    liq_lo, liq_hi = _clip_table(table_ranges, "liquidations", a_start, a_end)
    result.coverage = {
        "ticker_samples": {"first_ts": None if ticker_lo is None else ticker_lo.isoformat(), "last_ts": None if ticker_hi is None else ticker_hi.isoformat(), "row_count": int((table_ranges.get("ticker_samples") or {}).get("row_count") or 0)},
        "public_trades": {"first_ts": None if trade_lo is None else trade_lo.isoformat(), "last_ts": None if trade_hi is None else trade_hi.isoformat(), "row_count": int((table_ranges.get("public_trades") or {}).get("row_count") or 0)},
        "liquidations": {"first_ts": None if liq_lo is None else liq_lo.isoformat(), "last_ts": None if liq_hi is None else liq_hi.isoformat(), "row_count": int((table_ranges.get("liquidations") or {}).get("row_count") or 0)},
        "timeline_join": "ticker-based LEFT JOIN of trades/oi/liquidations",
    }
    try:
        ticker_stats: dict[str, Any] = {"sample_count": 0}
        trade_stats: dict[str, Any] = {"trade_count": 0, "total_notional": None, "buy_notional": None, "sell_notional": None, "vwap": None}
        if ticker_lo is not None and ticker_hi is not None:
            ticker_stats = query_ticker_window_stats(db, symbol=symbol, start=ticker_lo, end=ticker_hi)
            for tf in tfs:
                bars = query_price_bars(db, symbol=symbol, start=ticker_lo, end=ticker_hi, timeframe=tf)
                result.price_bars[tf] = bars
                result.oi_bars[tf] = oi_bars_from_price_bars(bars)
        else:
            result.warnings.append("no ticker_samples coverage in analysis window")
        if trade_lo is not None and trade_hi is not None:
            trade_stats = query_trade_vwap_window(db, symbol=symbol, start=trade_lo, end=trade_hi)
            for tf in tfs:
                result.tradeflow_bars[tf] = query_tradeflow_bars(db, symbol=symbol, start=trade_lo, end=trade_hi, timeframe=tf)
        else:
            result.warnings.append("no public_trades coverage in analysis window")
            for tf in tfs:
                result.tradeflow_bars[tf] = []
        if liq_lo is not None and liq_hi is not None:
            # Include last_ts: half-open end is exclusive, so bump by 1ms.
            # Also covers DateTime64(3) single-event clip where lo == hi.
            liq_end = liq_hi + timedelta(milliseconds=1)
            result.liquidations = query_liquidations(
                db, symbol=symbol, start=liq_lo, end=liq_end, tiny_notional=tiny_liquidation_notional
            )
            for tf in tfs:
                result.liquidation_bars[tf] = query_liquidation_bars(
                    db, symbol=symbol, start=liq_lo, end=liq_end, timeframe=tf, tiny_notional=tiny_liquidation_notional
                )
        else:
            result.warnings.append("no liquidations coverage in analysis window")
            result.liquidations = []
            for tf in tfs:
                result.liquidation_bars[tf] = []
        for tf in tfs:
            result.timelines[tf] = build_timeline_rows(price_bars=result.price_bars.get(tf) or [], tradeflow_bars=result.tradeflow_bars.get(tf) or [], oi_bars=result.oi_bars.get(tf) or [], liquidation_bars=result.liquidation_bars.get(tf) or [])
        result.price_summary = build_price_summary(symbol=symbol, ticker_stats=ticker_stats, price_bars_by_tf=result.price_bars, trade_stats=trade_stats)
        buy_n, sell_n, total_n = _dec(trade_stats.get("buy_notional")), _dec(trade_stats.get("sell_notional")), _dec(trade_stats.get("total_notional"))
        liq_total = sum((_dec(e.get("notional")) or Decimal("0")) for e in result.liquidations)
        result.stats = {
            "market_context_requested": True, "market_context_ok": True, "bar_timeframes": list(tfs),
            "ticker_rows": int(ticker_stats.get("sample_count") or 0), "trade_rows": int(trade_stats.get("trade_count") or 0), "liquidation_rows": len(result.liquidations),
            "price_bars_1m": len(result.price_bars.get("1m") or []), "price_bars_5m": len(result.price_bars.get("5m") or []),
            "tradeflow_bars_1m": len(result.tradeflow_bars.get("1m") or []), "tradeflow_bars_5m": len(result.tradeflow_bars.get("5m") or []),
            "oi_bars_1m": len(result.oi_bars.get("1m") or []), "oi_bars_5m": len(result.oi_bars.get("5m") or []),
            "liquidation_bars_1m": len(result.liquidation_bars.get("1m") or []), "liquidation_bars_5m": len(result.liquidation_bars.get("5m") or []),
            "timeline_rows_1m": len(result.timelines.get("1m") or []), "timeline_rows_5m": len(result.timelines.get("5m") or []),
            "price_start": ticker_stats.get("start_price"), "price_end": ticker_stats.get("end_price"), "price_change_pct": ticker_stats.get("net_change_pct"),
            "price_high": ticker_stats.get("high_price"), "price_low": ticker_stats.get("low_price"),
            "trade_total_notional": _fmt_dec(total_n), "trade_buy_notional": _fmt_dec(buy_n), "trade_sell_notional": _fmt_dec(sell_n),
            "trade_delta_notional": _fmt_dec(None if buy_n is None or sell_n is None else buy_n - sell_n),
            "oi_start": ticker_stats.get("oi_start"), "oi_end": ticker_stats.get("oi_end"), "oi_change_pct": ticker_stats.get("oi_change_pct"),
            "liquidation_event_count": len(result.liquidations), "liquidation_total_notional": _fmt_dec(liq_total),
            "tiny_liquidation_count": sum(1 for e in result.liquidations if e.get("is_tiny_event")),
            "tiny_liquidation_notional": format(tiny_liquidation_notional, "f"),
            "max_bar_range_pct": float(max_bar_range_pct),
            "max_oi_open_close_ratio": float(max_oi_open_close_ratio),
        }
        if int(ticker_stats.get("sample_count") or 0) == 0 and int(trade_stats.get("trade_count") or 0) == 0 and not result.liquidations:
            result.ok = False
            result.stats["market_context_ok"] = False
            result.error_message = "no ticker/trade/liquidation rows in window"
    except Exception as exc:  # noqa: BLE001
        logger.exception("market context aggregation failed")
        result.ok = False
        result.error_message = str(exc)
        result.stats = {"market_context_requested": True, "market_context_ok": False, "error_message": str(exc)}
    return result


def decide_phase3_market(*, ok: bool, has_partial_coverage: bool) -> str:
    if not ok:
        return "FULL_HISTORY_MARKET_CONTEXT_FAILED"
    if has_partial_coverage:
        return "FULL_HISTORY_MARKET_CONTEXT_PARTIAL"
    return "FULL_HISTORY_MARKET_CONTEXT_COMPLETE"


def decide_combined_analysis(*, run_replay: bool, run_market: bool, phase01_decision: str, replay_decision: str | None, market_decision: str | None, gap_count: int) -> str:
    if run_replay and run_market:
        r = replay_decision or "FULL_HISTORY_SEGMENT_REPLAY_FAILED"
        m = market_decision or "FULL_HISTORY_MARKET_CONTEXT_FAILED"
        if r.endswith("_FAILED") and m.endswith("_FAILED"):
            return "FULL_HISTORY_ANALYSIS_FAILED"
        if r.endswith("_FAILED") or m.endswith("_FAILED") or "PARTIAL" in r or "PARTIAL" in m:
            return "FULL_HISTORY_ANALYSIS_PARTIAL"
        if gap_count > 0 or "WITH_GAPS" in r or "WITH_GAPS" in phase01_decision:
            return "FULL_HISTORY_ANALYSIS_COMPLETE_WITH_GAPS"
        return "FULL_HISTORY_ANALYSIS_COMPLETE"
    if run_market and not run_replay:
        return market_decision or "FULL_HISTORY_MARKET_CONTEXT_FAILED"
    if run_replay and not run_market:
        return replay_decision or "FULL_HISTORY_SEGMENT_REPLAY_FAILED"
    return phase01_decision


def vwap_bounds_epsilon(
    *,
    vwap: float,
    min_price: float,
    max_price: float,
    absolute_epsilon: float = DEFAULT_VWAP_ABS_EPSILON,
    relative_epsilon: float = DEFAULT_VWAP_REL_EPSILON,
) -> float:
    """Numeric slack for float VWAP vs trade min/max (not a trading tolerance)."""
    scale = max(abs(min_price), abs(max_price), abs(vwap), 1.0)
    return max(absolute_epsilon, relative_epsilon * scale)


def vwap_within_trade_price_bounds(
    *,
    vwap: float,
    min_price: float,
    max_price: float,
    absolute_epsilon: float = DEFAULT_VWAP_ABS_EPSILON,
    relative_epsilon: float = DEFAULT_VWAP_REL_EPSILON,
) -> tuple[bool, float]:
    """Return (ok, epsilon) for VWAP vs ``[min_price, max_price]`` with float slack."""
    if not math.isfinite(vwap) or not math.isfinite(min_price) or not math.isfinite(max_price):
        return False, float("nan")
    eps = vwap_bounds_epsilon(
        vwap=vwap,
        min_price=min_price,
        max_price=max_price,
        absolute_epsilon=absolute_epsilon,
        relative_epsilon=relative_epsilon,
    )
    ok = (vwap >= min_price - eps) and (vwap <= max_price + eps)
    return ok, eps


def check_market_context_integrity(
    *,
    price_bars: Mapping[str, Sequence[Mapping[str, Any]]],
    tradeflow_bars: Mapping[str, Sequence[Mapping[str, Any]]],
    timelines: Mapping[str, Sequence[Mapping[str, Any]]],
    stats: Mapping[str, Any],
    max_bar_range_pct: float | None = None,
    max_oi_open_close_ratio: float | None = None,
    price_summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Symbol-agnostic plausibility guards (detect cross-symbol aggregation)."""
    errs, warns = [], []
    range_limit = float(
        max_bar_range_pct
        if max_bar_range_pct is not None
        else stats.get("max_bar_range_pct", DEFAULT_MAX_BAR_RANGE_PCT)
    )
    oi_ratio_limit = float(
        max_oi_open_close_ratio
        if max_oi_open_close_ratio is not None
        else stats.get("max_oi_open_close_ratio", DEFAULT_MAX_OI_OPEN_CLOSE_RATIO)
    )
    for tf, bars in price_bars.items():
        starts = [b.get("bucket_start") for b in bars]
        if len(starts) != len(set(starts)):
            errs.append(f"duplicate price bucket_start in {tf}")
        prev = None
        for b in bars:
            bs = b.get("bucket_start")
            if prev is not None and bs is not None and str(bs) < str(prev):
                errs.append(f"price bars not chronological in {tf}")
                break
            prev = bs
            o, h, l, c = _dec(b.get("open_price")), _dec(b.get("high_price")), _dec(b.get("low_price")), _dec(b.get("close_price"))
            if None not in (o, h, l, c):
                if not (l <= o <= h and l <= c <= h):
                    errs.append(f"OHLC invariant failed {tf} {bs}")
                if l > 0:
                    hl_ratio = float(h / l)
                    # hard ceiling: high/low cannot exceed (1 + max_bar_range_pct/100)
                    # e.g. default 20% → ratio > 1.2 fails (catches cross-symbol mixes)
                    if hl_ratio > 1.0 + range_limit / 100.0:
                        errs.append(
                            f"high/low ratio {hl_ratio:.6g} exceeds "
                            f"1+max_bar_range_pct/100 in {tf} {bs}"
                        )
            range_pct = _safe_float(b.get("range_pct"))
            if range_pct is not None and abs(range_pct) > range_limit:
                errs.append(
                    f"range_pct {range_pct:.4g} exceeds max_bar_range_pct={range_limit} in {tf} {bs}"
                )
            oi_o, oi_c = _dec(b.get("open_interest_open")), _dec(b.get("open_interest_close"))
            if oi_o is not None and oi_c is not None and oi_o > 0 and oi_c > 0:
                oi_ratio = float(max(oi_o, oi_c) / min(oi_o, oi_c))
                if oi_ratio > oi_ratio_limit:
                    errs.append(
                        f"OI open/close ratio {oi_ratio:.4g} exceeds "
                        f"max_oi_open_close_ratio={oi_ratio_limit} in {tf} {bs}"
                    )
            for key in ("price_change_pct", "spread_bps_close", "range_pct"):
                v = b.get(key)
                if isinstance(v, float) and (math.isinf(v) or math.isnan(v)):
                    errs.append(f"non-finite {key} in price {tf} {bs}")
    for tf, bars in tradeflow_bars.items():
        for b in bars:
            buy = _dec(b.get("buy_notional")) or Decimal("0")
            sell = _dec(b.get("sell_notional")) or Decimal("0")
            total = _dec(b.get("total_notional")) or Decimal("0")
            delta = _dec(b.get("delta_notional"))
            if delta is not None and abs(delta - (buy - sell)) > Decimal("0.0000001"):
                errs.append(f"delta_notional mismatch {tf} {b.get('bucket_start')}")
            if total > 0 and abs((buy + sell) - total) > Decimal("0.01"):
                warns.append(f"buy+sell != total notional {tf} {b.get('bucket_start')}")
            bs_share, ss_share = _safe_float(b.get("buy_share")), _safe_float(b.get("sell_share"))
            if total > 0 and bs_share is not None and ss_share is not None and abs(bs_share + ss_share - 1.0) > 1e-4:
                errs.append(f"buy_share+sell_share != 1 {tf} {b.get('bucket_start')}")
            vwap = _safe_float(b.get("vwap"))
            min_p, max_p = _dec(b.get("min_trade_price")), _dec(b.get("max_trade_price"))
            qty = _dec(b.get("total_quantity")) or Decimal("0")
            if vwap is not None and qty > 0 and min_p is not None and max_p is not None and min_p > 0:
                lo, hi = float(min_p), float(max_p)
                ok_vwap, eps = vwap_within_trade_price_bounds(vwap=vwap, min_price=lo, max_price=hi)
                if not ok_vwap:
                    errs.append(
                        f"vwap {vwap} outside [min_trade_price={lo}, max_trade_price={hi}] "
                        f"epsilon={eps} in {tf} {b.get('bucket_start')}"
                    )
    for tf, rows in timelines.items():
        if len([r.get("bucket_start") for r in rows]) != len({r.get("bucket_start") for r in rows}):
            errs.append(f"duplicate timeline buckets in {tf}")
        price_by = {b.get("bucket_start"): b for b in price_bars.get(tf) or []}
        for r in rows:
            pb = price_by.get(r.get("bucket_start"))
            if pb is None:
                errs.append(f"timeline bucket missing in price_bars {tf}")
            elif r.get("close_price") != pb.get("close_price"):
                errs.append(f"timeline close mismatch {tf} {r.get('bucket_start')}")
    if int(stats.get("price_bars_1m") or 0) != len(price_bars.get("1m") or []):
        errs.append("summary price_bars_1m count mismatch")
    if int(stats.get("timeline_rows_1m") or 0) != len(timelines.get("1m") or []):
        errs.append("summary timeline_rows_1m count mismatch")
    if price_summary:
        sh, sl = _dec(price_summary.get("high_price")), _dec(price_summary.get("low_price"))
        bar_highs = [_dec(b.get("high_price")) for bars in price_bars.values() for b in bars]
        bar_lows = [_dec(b.get("low_price")) for bars in price_bars.values() for b in bars]
        bar_highs_f = [v for v in bar_highs if v is not None]
        bar_lows_f = [v for v in bar_lows if v is not None]
        if sh is not None and bar_highs_f and sh < max(bar_highs_f):
            errs.append("summary high_price below bar high_price")
        if sl is not None and bar_lows_f and sl > min(bar_lows_f):
            errs.append("summary low_price above bar low_price")
        if sh is not None and sl is not None and sl > 0:
            span_pct = _safe_float((sh - sl) / sl * Decimal("100"))
            if span_pct is not None and span_pct > range_limit * 50:
                # summary-level absurd span (symbol mix across window)
                errs.append(f"summary high/low span_pct {span_pct:.4g} absurd vs max_bar_range_pct")
    return {"ok": len(errs) == 0, "errors": errs, "warnings": warns}


def phase3_output_files(timeframes: Sequence[str]) -> list[str]:
    files = list(PHASE3_OUTPUT_BASE)
    for tf in timeframes:
        files.extend([f"price_bars_{tf}.csv", f"tradeflow_{tf}.csv", f"oi_{tf}.csv", f"liquidation_bars_{tf}.csv", f"analysis_timeline_{tf}.csv"])
    return files


def quadrant_counts(oi_bars: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = {k: 0 for k in ("PRICE_UP_OI_UP", "PRICE_UP_OI_DOWN", "PRICE_DOWN_OI_UP", "PRICE_DOWN_OI_DOWN", "PRICE_FLAT", "OI_FLAT", "UNKNOWN")}
    for r in oi_bars:
        q = str(r.get("context_quadrant") or "UNKNOWN")
        counts[q] = counts.get(q, 0) + 1
    return counts

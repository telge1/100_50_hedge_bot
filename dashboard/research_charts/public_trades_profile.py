"""Read-only ClickHouse adapter for visible-range volume profiles.

Table: orderbook_analysis.public_trades_canonical
Engine: ReplacingMergeTree(ingest_timestamp) ORDER BY (symbol, trade_id)

Dedup: FINAL on the symbol + time window only (not a full-table scan).
The latest ingest_timestamp wins for a (symbol, trade_id) pair.

Does not read orderbook_deltas.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import clickhouse_connect
from clickhouse_connect.driver.exceptions import DatabaseError, OperationalError

from .clickhouse_config import load_clickhouse_config
from .volume_profile import (
    MAX_RANGE_SECONDS,
    NO_PUBLIC_TRADE_SYMBOLS,
    TradeRow,
    _dec,
    classify_coverage,
    coverage_label,
    empty_profile,
    make_empty_bins,
    profile_from_bins,
    resolve_rows,
    unix_utc,
)

CANONICAL_FQN = "orderbook_analysis.public_trades_canonical"
QUERY_TIMEOUT_S = 5
QUERY_MEMORY_BYTES = 120_000_000
QUERY_THREADS = 2
_CACHE_MAX = 64
_HIST_TTL = 45.0
_LIVE_TTL = 2.0
_COVERAGE_TTL = 30.0

_cache_lock = threading.Lock()
_profile_cache: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
_coverage_cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _client():
    cfg = load_clickhouse_config()
    return clickhouse_connect.get_client(**cfg.connect_kwargs())


def _qsettings() -> dict[str, int]:
    return {
        "max_execution_time": QUERY_TIMEOUT_S,
        "max_memory_usage": QUERY_MEMORY_BYTES,
        "max_threads": QUERY_THREADS,
    }


class VolumeProfileQueryError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 503):
        super().__init__(message)
        self.code = code
        self.status = status


def _translate_ch(exc: Exception) -> VolumeProfileQueryError:
    text = str(exc)
    if "241" in text or "MEMORY_LIMIT" in text:
        return VolumeProfileQueryError(
            "query_memory", "ClickHouse query memory limit exceeded", status=503
        )
    if "TIMEOUT" in text.upper() or "max_execution_time" in text or "159" in text:
        return VolumeProfileQueryError("query_timeout", "ClickHouse query timeout", status=503)
    return VolumeProfileQueryError("query_failed", "ClickHouse query failed", status=503)


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


def clear_volume_profile_cache_for_tests() -> None:
    with _cache_lock:
        _profile_cache.clear()
        _coverage_cache.clear()


def symbol_coverage(symbol: str) -> dict[str, Any]:
    sym = str(symbol or "").strip().upper()
    now = time.monotonic()
    with _cache_lock:
        hit = _coverage_cache.get(sym)
        if hit and now < hit[0]:
            return dict(hit[1])
    if sym in NO_PUBLIC_TRADE_SYMBOLS:
        empty = {
            "symbol": sym,
            "coverage_start": None,
            "coverage_end": None,
            "gap_start": None,
            "gap_end": None,
            "available": False,
        }
        with _cache_lock:
            _coverage_cache[sym] = (now + _COVERAGE_TTL, empty)
        return dict(empty)

    sql = f"""
        SELECT source, min(trade_ts), max(trade_ts)
        FROM {CANONICAL_FQN}
        PREWHERE symbol = {{symbol:String}}
        GROUP BY source
    """
    client = _client()
    try:
        result = client.query(sql, parameters={"symbol": sym}, settings=_qsettings())
    except (DatabaseError, OperationalError) as exc:
        raise _translate_ch(exc) from exc
    finally:
        client.close()

    by_source: dict[str, tuple[datetime, datetime]] = {}
    for source, first, last in result.result_rows:
        if first is None or last is None:
            continue
        by_source[str(source)] = (_utc(first), _utc(last))
    starts = [p[0] for p in by_source.values()]
    ends = [p[1] for p in by_source.values()]
    archive = by_source.get("archive")
    live = by_source.get("live") or by_source.get("gap_fill")
    gap_start = gap_end = None
    if archive and live and live[0] > archive[1]:
        gap_start = archive[1]
        gap_end = live[0]
    payload = {
        "symbol": sym,
        "coverage_start": min(starts) if starts else None,
        "coverage_end": max(ends) if ends else None,
        "gap_start": gap_start,
        "gap_end": gap_end,
        "available": bool(starts),
    }
    with _cache_lock:
        _coverage_cache[sym] = (time.monotonic() + _COVERAGE_TTL, payload)
        while len(_coverage_cache) > 256:
            _coverage_cache.popitem(last=False)
    return dict(payload)


def fetch_raw_trades_for_tests(symbol: str, start: datetime, end: datetime) -> list[TradeRow]:
    """Small-window helper for independent control tests. Not used by the API."""
    sql = f"""
        SELECT trade_id, price, size, notional, side, trade_ts
        FROM {CANONICAL_FQN} FINAL
        PREWHERE symbol = {{symbol:String}}
        WHERE trade_ts >= {{start:DateTime64(3, 'UTC')}}
          AND trade_ts <  {{end:DateTime64(3, 'UTC')}}
    """
    client = _client()
    try:
        result = client.query(
            sql,
            parameters={"symbol": symbol, "start": start, "end": end},
            settings=_qsettings(),
        )
    except (DatabaseError, OperationalError) as exc:
        raise _translate_ch(exc) from exc
    finally:
        client.close()
    return [
        TradeRow(
            trade_id=str(trade_id),
            price=Decimal(str(price)),
            size=Decimal(str(size)),
            notional=Decimal(str(notional)),
            side=str(side),
            trade_ts=_utc(trade_ts),
        )
        for trade_id, price, size, notional, side, trade_ts in result.result_rows
    ]


def _fetch_bounds(symbol: str, start: datetime, end: datetime) -> dict[str, Any]:
    sql = f"""
        SELECT
            min(price) AS pmin,
            max(price) AS pmax,
            min(trade_ts) AS tmin,
            max(trade_ts) AS tmax,
            count() AS n
        FROM {CANONICAL_FQN} FINAL
        PREWHERE symbol = {{symbol:String}}
        WHERE trade_ts >= {{start:DateTime64(3, 'UTC')}}
          AND trade_ts <  {{end:DateTime64(3, 'UTC')}}
    """
    client = _client()
    try:
        result = client.query(
            sql,
            parameters={"symbol": symbol, "start": start, "end": end},
            settings=_qsettings(),
        )
    except (DatabaseError, OperationalError) as exc:
        raise _translate_ch(exc) from exc
    finally:
        client.close()
    row = result.result_rows[0]
    return {
        "pmin": row[0],
        "pmax": row[1],
        "tmin": _utc(row[2]) if row[2] is not None else None,
        "tmax": _utc(row[3]) if row[3] is not None else None,
        "n": int(row[4] or 0),
    }


def _fetch_bin_rows(
    symbol: str,
    start: datetime,
    end: datetime,
    pmin: Decimal,
    pmax: Decimal,
    rows: int,
) -> list[tuple]:
    sql = f"""
        SELECT
            least(
                {{rows:UInt32}} - 1,
                greatest(
                    0,
                    toInt32(
                        if(
                            {{pmax:Float64}} = {{pmin:Float64}},
                            0,
                            floor(
                                (toFloat64(price) - {{pmin:Float64}})
                                / ({{pmax:Float64}} - {{pmin:Float64}})
                                * {{rows:UInt32}}
                            )
                        )
                    )
                )
            ) AS bin,
            sumIf(toFloat64(size), side = 'Buy') AS buy_base,
            sumIf(toFloat64(size), side = 'Sell') AS sell_base,
            sumIf(toFloat64(notional), side = 'Buy') AS buy_quote,
            sumIf(toFloat64(notional), side = 'Sell') AS sell_quote,
            countIf(side = 'Buy') AS buy_n,
            countIf(side = 'Sell') AS sell_n
        FROM {CANONICAL_FQN} FINAL
        PREWHERE symbol = {{symbol:String}}
        WHERE trade_ts >= {{start:DateTime64(3, 'UTC')}}
          AND trade_ts <  {{end:DateTime64(3, 'UTC')}}
        GROUP BY bin
        ORDER BY bin
    """
    client = _client()
    try:
        result = client.query(
            sql,
            parameters={
                "symbol": symbol,
                "start": start,
                "end": end,
                "pmin": float(pmin),
                "pmax": float(pmax),
                "rows": int(rows),
            },
            settings=_qsettings(),
        )
    except (DatabaseError, OperationalError) as exc:
        raise _translate_ch(exc) from exc
    finally:
        client.close()
    return list(result.result_rows)


def _fmt(ts: datetime | None) -> str:
    if ts is None:
        return "—"
    return ts.strftime("%Y-%m-%d %H:%M UTC")


def _empty_payload(
    symbol: str,
    start: datetime,
    end: datetime,
    rows: int,
    volume_mode: str,
    *,
    coverage_code: str,
    reason: str,
    coverage_available: bool,
    warning: str | None,
    cov: dict[str, Any] | None = None,
) -> dict[str, Any]:
    profile = empty_profile(rows=rows, volume_mode=volume_mode)
    cov = cov or {}
    return {
        "success": True,
        "symbol": symbol,
        "requested_start": unix_utc(start),
        "requested_end": unix_utc(end),
        "effective_start": None,
        "effective_end": None,
        "coverage_start": unix_utc(cov["coverage_start"]) if cov.get("coverage_start") else None,
        "coverage_end": unix_utc(cov["coverage_end"]) if cov.get("coverage_end") else None,
        "coverage_complete": False,
        "coverage_code": coverage_code,
        "coverage_reason": reason,
        "coverage_available": coverage_available,
        "coverage_label": coverage_label(coverage_code),
        "warning": warning,
        "source": CANONICAL_FQN,
        "dedup": "replacing_mergetree_final_window_symbol_trade_id",
        "dedup_selection": "latest_ingest_timestamp",
        "field_conflicts": None,
        **profile,
    }


def load_volume_profile(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    rows: object = "auto",
    volume_mode: str = "base",
    known_symbol: bool = True,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    sym = str(symbol or "").strip().upper()
    if not sym:
        raise ValueError("invalid_symbol")
    if volume_mode not in ("base", "quote"):
        raise ValueError("invalid_volume_mode")
    n_rows = resolve_rows(rows)
    start = _utc(start)
    end = _utc(end)
    if start >= end:
        raise ValueError("invalid_time_range")
    span = (end - start).total_seconds()
    if span > MAX_RANGE_SECONDS:
        raise ValueError("time_range_too_large")
    if not known_symbol and sym not in NO_PUBLIC_TRADE_SYMBOLS:
        raise KeyError(sym)

    req_start_unix = unix_utc(start)
    req_end_unix = unix_utc(end)
    cache_key = (sym, req_start_unix, req_end_unix, n_rows, volume_mode)
    hit = _cache_get(cache_key)
    if hit is not None:
        hit["computation_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        return hit

    if sym in NO_PUBLIC_TRADE_SYMBOLS:
        payload = _empty_payload(
            sym,
            start,
            end,
            n_rows,
            volume_mode,
            coverage_code="NONE",
            reason="symbol_has_no_public_trades",
            coverage_available=False,
            warning="XAUUSDT is candles/demand-only; no public trades",
        )
        payload["computation_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        payload["cached"] = False
        _cache_put(cache_key, payload, _HIST_TTL)
        return payload

    cov = symbol_coverage(sym)
    query_start, query_end = start, end
    cov_start = cov.get("coverage_start")
    cov_end = cov.get("coverage_end")
    if cov_start and query_start < cov_start:
        query_start = cov_start
    if cov_end and query_end > cov_end:
        query_end = cov_end + timedelta(milliseconds=1)
    if query_start >= query_end:
        code, reason = classify_coverage(
            requested_start=start,
            requested_end=end,
            coverage_start=cov_start,
            coverage_end=cov_end,
            gap_start=cov.get("gap_start"),
            gap_end=cov.get("gap_end"),
            trade_count=0,
        )
        warning = coverage_label(code)
        payload = _empty_payload(
            sym,
            start,
            end,
            n_rows,
            volume_mode,
            coverage_code=code,
            reason=reason,
            coverage_available=bool(cov.get("available")),
            warning=warning,
            cov=cov,
        )
        payload["computation_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        payload["cached"] = False
        _cache_put(cache_key, payload, _HIST_TTL)
        return payload

    bounds = _fetch_bounds(sym, query_start, query_end)
    trade_count = int(bounds["n"])
    code, reason = classify_coverage(
        requested_start=start,
        requested_end=end,
        coverage_start=cov.get("coverage_start"),
        coverage_end=cov.get("coverage_end"),
        gap_start=cov.get("gap_start"),
        gap_end=cov.get("gap_end"),
        trade_count=trade_count,
    )
    if trade_count <= 0 or bounds["pmin"] is None:
        profile = empty_profile(rows=n_rows, volume_mode=volume_mode)
        effective_start = None
        effective_end = None
    else:
        pmin = _dec(bounds["pmin"])
        pmax = _dec(bounds["pmax"])
        n = 1 if pmin == pmax else n_rows
        dense = make_empty_bins(pmin, pmax, n)
        for bin_i, buy_b, sell_b, buy_q, sell_q, buy_n, sell_n in _fetch_bin_rows(
            sym, query_start, query_end, pmin, pmax, n
        ):
            idx = int(bin_i)
            if idx < 0 or idx >= n:
                continue
            b = dense[idx]
            b.buy_base = _dec(buy_b)
            b.sell_base = _dec(sell_b)
            b.buy_quote = _dec(buy_q)
            b.sell_quote = _dec(sell_q)
            b.buy_count = int(buy_n)
            b.sell_count = int(sell_n)
        profile = profile_from_bins(
            dense,
            rows_requested=n_rows,
            volume_mode=volume_mode,
            price_min=pmin,
            price_max=pmax,
        )
        effective_start = bounds["tmin"]
        effective_end = bounds["tmax"]
    now = datetime.now(timezone.utc)
    live_tail = end >= now - timedelta(seconds=120)
    warning = None
    if code == "PARTIAL":
        warning = (
            f"Profil teilweise: {_fmt(effective_start)}–{_fmt(effective_end)}"
            if effective_start and effective_end
            else "Profil teilweise"
        )
    elif code == "GAP_OPEN":
        warning = "Cutover-Lücke vorhanden"
    elif code == "NONE":
        warning = "Keine Public-Trade-Daten"

    payload = {
        "success": True,
        "symbol": sym,
        "requested_start": req_start_unix,
        "requested_end": req_end_unix,
        "effective_start": unix_utc(effective_start) if effective_start else None,
        "effective_end": unix_utc(effective_end) if effective_end else None,
        "coverage_start": unix_utc(cov["coverage_start"]) if cov.get("coverage_start") else None,
        "coverage_end": unix_utc(cov["coverage_end"]) if cov.get("coverage_end") else None,
        "coverage_complete": code == "FULL",
        "coverage_code": code,
        "coverage_reason": reason,
        "coverage_available": bool(cov.get("available")),
        "coverage_label": coverage_label(code),
        "warning": warning,
        "cached": False,
        "source": CANONICAL_FQN,
        "dedup": "replacing_mergetree_final_window_symbol_trade_id",
        "dedup_selection": "latest_ingest_timestamp",
        "field_conflicts": None,
        **profile,
    }
    ttl = _LIVE_TTL if live_tail else _HIST_TTL
    _cache_put(cache_key, payload, ttl)
    payload["computation_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    return payload

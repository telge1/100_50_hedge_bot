"""Grouped read-only candle coverage for the tradeable-51 universe.

Does not write ClickHouse, does not read signals, does not start processes.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import (
    EXCHANGE,
    FRESHNESS_GRACE_MINUTES,
    INTERVAL,
    REQUESTED_FROM,
    cache_ttl_seconds,
    freshness_grace_minutes,
    jobs_root,
    universe_path,
)
from .universe import load_tradeable_51, universe_meta

STATUS_FULL = "FULL"
STATUS_LISTING_LIMITED = "LISTING_LIMITED"
STATUS_INCOMPLETE = "INCOMPLETE"
STATUS_NO_DATA = "NO_DATA"

FRESHNESS_CURRENT = "CURRENT"
FRESHNESS_UPDATE_AVAILABLE = "UPDATE_AVAILABLE"
FRESHNESS_NO_DATA = "NO_DATA"

_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"expires": 0.0, "payload": None, "generation": 0}


def coverage_generation(environ: dict | None = None) -> int:
    path = jobs_root(environ) / "coverage_generation.txt"
    try:
        return int(path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def bump_coverage_generation(environ: dict | None = None) -> int:
    root = jobs_root(environ)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "coverage_generation.txt"
    nxt = coverage_generation(environ) + 1
    tmp = path.with_suffix(".txt.tmp")
    tmp.write_text(str(nxt), encoding="utf-8")
    tmp.replace(path)
    clear_coverage_cache()
    return nxt


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def iso_z(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    return _ensure_utc(ts).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def inclusive_minute_count(start: datetime, end: datetime) -> int:
    start = _ensure_utc(start).replace(second=0, microsecond=0)
    end = _ensure_utc(end).replace(second=0, microsecond=0)
    if end < start:
        return 0
    return int((end - start).total_seconds() // 60) + 1


def last_closed_open_time(now: datetime | None = None) -> datetime:
    """Last fully closed 1m open_time in UTC. Independent of coverage FULL."""
    current = _ensure_utc(now or datetime.now(timezone.utc))
    floored = current.replace(second=0, microsecond=0)
    return floored - timedelta(minutes=1)


def apply_freshness(
    row: dict[str, Any],
    *,
    freshness_reference: datetime,
    data_to: datetime | None,
    grace_minutes: int | None = None,
) -> dict[str, Any]:
    ref = _ensure_utc(freshness_reference).replace(second=0, microsecond=0)
    row["freshness_reference"] = iso_z(ref)
    grace = freshness_grace_minutes() if grace_minutes is None else max(0, int(grace_minutes))
    if data_to is None:
        row["freshness_status"] = FRESHNESS_NO_DATA
        row["lag_minutes"] = None
        row["update_from"] = None
        return row
    end = _ensure_utc(data_to).replace(second=0, microsecond=0)
    lag = int((ref - end).total_seconds() // 60)
    if lag < 0:
        lag = 0
    row["lag_minutes"] = lag
    if lag <= grace:
        row["freshness_status"] = FRESHNESS_CURRENT
        row["update_from"] = None
        return row
    row["freshness_status"] = FRESHNESS_UPDATE_AVAILABLE
    row["update_from"] = iso_z(end + timedelta(minutes=1))
    return row


def days_available(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    start = _ensure_utc(start).replace(second=0, microsecond=0)
    end = _ensure_utc(end).replace(second=0, microsecond=0)
    if end < start:
        return 0
    return int((end - start).total_seconds() // 86400)


def classify_symbol(
    *,
    symbol: str,
    requested_from: datetime,
    data_from: datetime | None,
    data_to: datetime | None,
    candle_count: int,
    uniq_open: int | None = None,
) -> dict[str, Any]:
    logical = int(uniq_open if uniq_open is not None else candle_count)
    row: dict[str, Any] = {
        "symbol": str(symbol).strip().upper(),
        "requested_from": iso_z(requested_from),
        "data_from": iso_z(data_from),
        "data_to": iso_z(data_to),
        "days_available": days_available(data_from, data_to),
        "candle_count": logical,
        "expected_count": 0,
        "missing_count": 0,
        "coverage_status": STATUS_NO_DATA,
        "testable": False,
        "freshness_status": FRESHNESS_NO_DATA,
        "freshness_reference": None,
        "lag_minutes": None,
        "update_from": None,
    }
    if data_from is None or data_to is None or logical <= 0:
        return row

    listing_limited = _ensure_utc(data_from) > _ensure_utc(requested_from)
    span_start = data_from if listing_limited else requested_from
    expected = inclusive_minute_count(span_start, data_to)
    missing = max(0, expected - logical)
    row["expected_count"] = expected
    row["missing_count"] = missing
    if missing > 0:
        row["coverage_status"] = STATUS_INCOMPLETE
        row["testable"] = False
        return row
    if listing_limited:
        row["coverage_status"] = STATUS_LISTING_LIMITED
        row["testable"] = True
        return row
    row["coverage_status"] = STATUS_FULL
    row["testable"] = True
    return row


COVERAGE_SQL = """
SELECT
    symbol,
    min(open_time) AS data_from,
    max(open_time) AS data_to,
    count() AS candle_count,
    uniqExact(open_time) AS uniq_open
FROM {database}.{table} FINAL
WHERE exchange = {{exchange:String}}
  AND interval = {{interval:String}}
  AND is_closed = 1
  AND open_time >= {{requested_from:DateTime64(3, 'UTC')}}
  AND symbol IN {{symbols:Array(String)}}
GROUP BY symbol
"""


def grouped_coverage_sql(database: str, table: str) -> str:
    return COVERAGE_SQL.format(database=database, table=table)


def _fetch_rows(
    *,
    symbols: list[str],
    requested_from: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from research_charts.clickhouse_config import load_clickhouse_config

    cfg = load_clickhouse_config()
    import clickhouse_connect

    sql = grouped_coverage_sql(cfg.database, cfg.table)
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    try:
        result = client.query(
            sql,
            parameters={
                "exchange": cfg.exchange or EXCHANGE,
                "interval": INTERVAL,
                "requested_from": _ensure_utc(requested_from),
                "symbols": symbols,
            },
        )
        fetched = [dict(zip(result.column_names, row, strict=True)) for row in result.result_rows]
    finally:
        client.close()
    meta = {
        "database": cfg.database,
        "table": cfg.table,
        "exchange": cfg.exchange or EXCHANGE,
        "interval": INTERVAL,
        "final": True,
        "is_closed": 1,
        "read_only": True,
    }
    return fetched, meta


def _as_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _ensure_utc(value)
    return None


def assemble_coins(
    symbols: list[str],
    fetched: list[dict[str, Any]],
    *,
    requested_from: datetime,
    freshness_reference: datetime | None = None,
    grace_minutes: int | None = None,
) -> list[dict[str, Any]]:
    ref = freshness_reference if freshness_reference is not None else last_closed_open_time()
    by_sym = {str(r.get("symbol") or "").strip().upper(): r for r in fetched}
    coins: list[dict[str, Any]] = []
    for symbol in symbols:
        raw = by_sym.get(symbol)
        if not raw:
            row = classify_symbol(
                symbol=symbol,
                requested_from=requested_from,
                data_from=None,
                data_to=None,
                candle_count=0,
            )
            coins.append(
                apply_freshness(
                    row, freshness_reference=ref, data_to=None, grace_minutes=grace_minutes
                )
            )
            continue
        data_to = _as_dt(raw.get("data_to"))
        row = classify_symbol(
            symbol=symbol,
            requested_from=requested_from,
            data_from=_as_dt(raw.get("data_from")),
            data_to=data_to,
            candle_count=int(raw.get("candle_count") or 0),
            uniq_open=int(raw["uniq_open"]) if raw.get("uniq_open") is not None else None,
        )
        coins.append(
            apply_freshness(
                row, freshness_reference=ref, data_to=data_to, grace_minutes=grace_minutes
            )
        )
    return coins


def build_payload(
    *,
    symbols: list[str],
    coins: list[dict[str, Any]],
    clickhouse: dict[str, Any],
    universe: dict[str, Any],
    requested_from: datetime,
    error: str | None = None,
    freshness_reference: datetime | None = None,
    freshness_grace_minutes: int | None = None,
) -> dict[str, Any]:
    as_of_candidates: list[str] = [str(c.get("data_to")) for c in coins if c.get("data_to")]
    as_of = max(as_of_candidates) if as_of_candidates else None
    testable = sum(1 for c in coins if c.get("testable"))
    update_available = sum(
        1 for c in coins if c.get("freshness_status") == FRESHNESS_UPDATE_AVAILABLE
    )
    current = sum(1 for c in coins if c.get("freshness_status") == FRESHNESS_CURRENT)
    ref_iso = iso_z(freshness_reference) if freshness_reference is not None else None
    if ref_iso is None:
        refs = [str(c.get("freshness_reference")) for c in coins if c.get("freshness_reference")]
        ref_iso = refs[0] if refs else None
    return {
        "success": error is None,
        "read_only": True,
        "writes": False,
        "live_trading": False,
        "publish_latest": False,
        "signal_generation": False,
        "universe_path": universe.get("path"),
        "universe_count": len(symbols),
        "requested_from": iso_z(requested_from),
        "as_of": as_of,
        "freshness_reference": ref_iso,
        "freshness_grace_minutes": (
            int(FRESHNESS_GRACE_MINUTES)
            if freshness_grace_minutes is None
            else int(freshness_grace_minutes)
        ),
        "present": sum(1 for c in coins if c.get("coverage_status") != STATUS_NO_DATA),
        "testable": testable,
        "freshness_current": current,
        "update_available": update_available,
        "coins": coins,
        "clickhouse": clickhouse,
        "error": error,
        "message": error,
    }


def coverage_http_status(payload: dict[str, Any]) -> int:
    """Universe rows render on 200 even if ClickHouse failed. 503 only when no table."""
    if payload.get("coins"):
        return 200
    return 200 if payload.get("success") else 503


def coverage_report(
    *,
    use_cache: bool = True,
    environ: dict | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    ttl = cache_ttl_seconds(environ)
    cache_now = time.monotonic()
    generation = coverage_generation(environ)
    if use_cache and ttl > 0:
        with _cache_lock:
            payload = _cache.get("payload")
            cached_gen = int(_cache.get("generation") or 0)
            if (
                payload is not None
                and cached_gen == generation
                and float(_cache.get("expires") or 0) > cache_now
            ):
                return payload

    requested_from = REQUESTED_FROM
    grace = freshness_grace_minutes(environ)
    ch_meta = {
        "database": "signal_generator",
        "table": "candles_1m",
        "exchange": EXCHANGE,
        "interval": INTERVAL,
        "final": True,
        "is_closed": 1,
        "read_only": True,
    }
    try:
        path = universe_path(environ)
        symbols = load_tradeable_51(path)
        meta = universe_meta(path)
    except Exception as exc:  # noqa: BLE001
        return build_payload(
            symbols=[],
            coins=[],
            clickhouse=ch_meta,
            universe={"path": str(universe_path(environ)), "count": 0},
            requested_from=requested_from,
            error=f"Universe load failed: {exc}",
            freshness_reference=last_closed_open_time(now),
            freshness_grace_minutes=grace,
        )

    error = None
    fetched: list[dict[str, Any]] = []
    try:
        fetched, ch_meta = _fetch_rows(symbols=symbols, requested_from=requested_from)
    except Exception as exc:  # noqa: BLE001 — surface to UI, never write
        error = f"ClickHouse coverage query failed: {exc}"

    freshness_reference = last_closed_open_time(now)
    coins = assemble_coins(
        symbols,
        fetched,
        requested_from=requested_from,
        freshness_reference=freshness_reference,
        grace_minutes=grace,
    )
    payload = build_payload(
        symbols=symbols,
        coins=coins,
        clickhouse=ch_meta,
        universe=meta,
        requested_from=requested_from,
        error=error,
        freshness_reference=freshness_reference,
        freshness_grace_minutes=grace,
    )
    if use_cache and ttl > 0 and error is None:
        with _cache_lock:
            _cache["payload"] = payload
            _cache["expires"] = time.monotonic() + ttl
            _cache["generation"] = generation
    return payload


def clear_coverage_cache() -> None:
    with _cache_lock:
        _cache["payload"] = None
        _cache["expires"] = 0.0
        _cache["generation"] = -1

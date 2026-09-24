"""SELECT-only live EMA-59 band signals from the signal-generator MySQL store."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Sequence
from zoneinfo import ZoneInfo

from .config import load_ema_sg_db_config

BANNER_TITLE = "EMA 59 BAND · MULTIPLIKATOR 3"
BANNER_SUB = "Signal-Generator · 5m · Thresholds aus ClickHouse (17.09.2026)"
MISSING_MESSAGE = "Signal-Generator Datenbank nicht erreichbar"
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500

# Host where signal-generator historically wrote aware-UTC → local wall-clock.
_LEGACY_LOCAL_TZ = ZoneInfo("Europe/Paris")
# candle_time ahead of created_on by ~1h (CET) or ~2h (CEST) → mis-stored local.
_LEGACY_SKEW_MIN = 50
_LEGACY_SKEW_MAX = 130


def _parse_bound(value: str | None) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _sql_dt(value: str | None) -> str | None:
    dt = _parse_bound(value)
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _num(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return _parse_bound(str(value))


def _assume_utc(value: Any) -> datetime | None:
    """Normalize any stored value to aware UTC. Naive values are UTC by contract."""
    dt = _as_datetime(value)
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _correct_legacy_local(candle: datetime | None, created: datetime | None) -> datetime | None:
    """Fix candle/expected times stored as Europe/Paris wall-clock but labeled UTC."""
    if candle is None:
        return None
    ct = _assume_utc(candle)
    co = _assume_utc(created)
    if ct is None:
        return None
    if co is not None:
        delta_min = (ct.replace(tzinfo=None) - co.replace(tzinfo=None)).total_seconds() / 60.0
        if _LEGACY_SKEW_MIN <= delta_min <= _LEGACY_SKEW_MAX:
            local_naive = ct.replace(tzinfo=None)
            return local_naive.replace(tzinfo=_LEGACY_LOCAL_TZ).astimezone(timezone.utc)
    return ct


def _utc_label(value: Any) -> str:
    dt = _assume_utc(value)
    if dt is None:
        return "–"
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def _state_chip(state: str) -> str:
    u = (state or "").upper()
    if u == "CREATED":
        return "stoch-chip-open"
    if u == "COMPLETED":
        return "stoch-chip-tp"
    if u == "IN_PROGRESS":
        return "stoch-chip-pending"
    if u == "ERROR":
        return "stoch-chip-sl"
    return "stoch-chip-none"


def _fmt_price(value: Any) -> str:
    num = _num(value)
    if num is None:
        return "–"
    if abs(num) >= 100:
        return f"{num:.2f}"
    if abs(num) >= 1:
        return f"{num:.4f}"
    return f"{num:.6f}".rstrip("0").rstrip(".")


def _fmt_pct(value: Any) -> str:
    num = _num(value)
    if num is None:
        return "–"
    return f"{num:.2f}%"


def _display_row(row: dict[str, Any]) -> dict[str, Any]:
    created = _assume_utc(row.get("created_on"))
    candle = _correct_legacy_local(row.get("candle_time"), row.get("created_on"))
    ts = candle or created
    direction = str(row.get("trade_direction") or "").upper()
    state = str(row.get("signal_state") or "–")
    return {
        "signal_id": row.get("id"),
        "symbol": row.get("symbol"),
        "direction_label": direction,
        "signal_time_label": _utc_label(ts),
        "created_label": _utc_label(created),
        "state_label": state,
        "result_chip": _state_chip(state),
        "entry_price_label": _fmt_price(row.get("expected_open_price") or row.get("current_price")),
        "distance_label": _fmt_pct(row.get("distance")),
        "thr_label": _fmt_pct(row.get("expected_distance")),
        "signal_timeframe": "5m",
    }


def _where(
    *,
    symbol: str | None,
    direction: str | None,
    state: str | None,
    start_time: str | None,
    end_time: str | None,
) -> tuple[str, list[Any]]:
    clauses = ["1=1"]
    params: list[Any] = []
    want_symbol = str(symbol or "").strip().upper()
    if want_symbol:
        clauses.append("UPPER(symbol) = %s")
        params.append(want_symbol)
    want_direction = str(direction or "").strip().upper()
    if want_direction in ("LONG", "SHORT"):
        clauses.append("trade_direction = %s")
        params.append(want_direction)
    want_state = str(state or "").strip().upper()
    if want_state:
        clauses.append("signal_state = %s")
        params.append(want_state)
    start_sql = _sql_dt(start_time)
    if start_sql:
        clauses.append("COALESCE(candle_time, created_on) >= %s")
        params.append(start_sql)
    end_sql = _sql_dt(end_time)
    if end_sql:
        clauses.append("COALESCE(candle_time, created_on) <= %s")
        params.append(end_sql)
    return " AND ".join(clauses), params


def _clamp_page(page: int, page_size: int) -> tuple[int, int]:
    try:
        size = max(1, min(int(page_size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
    except (TypeError, ValueError):
        size = DEFAULT_PAGE_SIZE
    try:
        page_n = max(0, int(page or 0))
    except (TypeError, ValueError):
        page_n = 0
    return page_n, size


def empty_payload(message: str = MISSING_MESSAGE) -> dict[str, Any]:
    return {
        "success": True,
        "feed_ready": False,
        "message": message,
        "signals": [],
        "items": [],
        "symbols": [],
        "total": 0,
        "page": 0,
        "page_size": DEFAULT_PAGE_SIZE,
        "pagination": {
            "page": 0,
            "page_size": DEFAULT_PAGE_SIZE,
            "total_filtered": 0,
            "total_pages": 0,
            "has_prev": False,
            "has_next": False,
        },
        "page_summary": {"signals": 0, "created": 0, "completed": 0, "cancelled": 0},
        "banner": {"title": BANNER_TITLE, "body": BANNER_SUB},
        "window_label": "EMA59 ± 3σ · thr vom 17.09.2026",
    }


def _mysql_fetch(sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
    import pymysql
    from pymysql.cursors import DictCursor

    cfg = load_ema_sg_db_config()
    if cfg is None:
        raise RuntimeError("ema_sg_mysql_config_missing")
    conn = pymysql.connect(cursorclass=DictCursor, **cfg.connect_kwargs())
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return list(cur.fetchall() or [])
    finally:
        conn.close()


def paginated_live_signals(
    *,
    symbol: str | None = None,
    direction: str | None = None,
    state: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    page: int = 0,
    page_size: int = DEFAULT_PAGE_SIZE,
    fetch: Callable[[str, Sequence[Any]], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Live trade_signals from the EMA-distance signal-generator. SELECT only."""
    runner = fetch or _mysql_fetch
    page_n, size = _clamp_page(page, page_size)
    where_sql, where_params = _where(
        symbol=symbol,
        direction=direction,
        state=state,
        start_time=start_time,
        end_time=end_time,
    )
    try:
        total_rows = runner(
            f"SELECT COUNT(*) AS c FROM trade_signals WHERE {where_sql}",
            where_params,
        )
        state_rows = runner(
            f"SELECT signal_state, COUNT(*) AS c FROM trade_signals WHERE {where_sql} GROUP BY signal_state",
            where_params,
        )
        symbol_rows = runner(
            "SELECT DISTINCT symbol AS s FROM trade_signals "
            "WHERE symbol IS NOT NULL AND symbol <> '' ORDER BY symbol",
            (),
        )
        total = int((total_rows[0] or {}).get("c") or 0) if total_rows else 0
        total_pages = (total + size - 1) // size if size else 0
        if total_pages:
            page_n = min(page_n, total_pages - 1)
        offset = page_n * size
        rows = runner(
            "SELECT id, symbol, trade_direction, signal_state, created_on, candle_time, "
            "expected_open_price, current_price, distance, expected_distance "
            f"FROM trade_signals WHERE {where_sql} "
            "ORDER BY COALESCE(candle_time, created_on) DESC, id DESC "
            "LIMIT %s OFFSET %s",
            [*where_params, size, offset],
        )
    except Exception as exc:
        payload = empty_payload(f"{MISSING_MESSAGE}: {exc}")
        payload["error"] = str(exc)
        return payload

    counts = {str(r.get("signal_state") or "").upper(): int(r.get("c") or 0) for r in state_rows}
    mapped = [_display_row(r) for r in rows if isinstance(r, dict)]
    pagination = {
        "page": page_n,
        "page_size": size,
        "total_filtered": total,
        "total_filtered_trades": total,
        "total_pages": total_pages,
        "has_prev": page_n > 0,
        "has_next": page_n + 1 < total_pages,
    }
    return {
        "success": True,
        "feed_ready": True,
        "message": None,
        "signals": mapped,
        "items": mapped,
        "symbols": [str(r.get("s") or "").upper() for r in symbol_rows if r.get("s")],
        "total": total,
        "page": page_n,
        "page_size": size,
        "pagination": pagination,
        "page_summary": {
            "signals": total,
            "created": counts.get("CREATED", 0),
            "completed": counts.get("COMPLETED", 0),
            "cancelled": counts.get("CANCELLED", 0) + counts.get("ERROR", 0),
        },
        "banner": {"title": BANNER_TITLE, "body": BANNER_SUB},
        "window_label": "EMA59 ± 3σ · thr vom 17.09.2026",
    }

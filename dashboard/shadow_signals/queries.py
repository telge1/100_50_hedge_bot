"""SELECT-only ClickHouse queries. Table name from side whitelist only."""

from __future__ import annotations

import re
from typing import Any, Callable, Literal

from shadow_signals.config import LONG_VIEW, SHORT_VIEW

Side = Literal["long", "short"]

_WRITE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|REPLACE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)

TABLE_BY_SIDE: dict[Side, str] = {
    "long": LONG_VIEW,
    "short": SHORT_VIEW,
}


def normalize_side(raw: str | None) -> Side:
    value = str(raw or "long").strip().lower()
    if value == "short":
        return "short"
    return "long"


def table_for_side(side: Side) -> str:
    return TABLE_BY_SIDE[side]


def assert_select(sql: str) -> str:
    cleaned = " ".join(sql.split())
    upper = cleaned.upper()
    if not upper.startswith("SELECT"):
        raise RuntimeError("shadow signal queries must start with SELECT")
    if _WRITE.search(cleaned):
        raise RuntimeError("shadow signal queries may not contain write DDL/DML")
    return sql


class SelectOnlyExecutor:
    def __init__(self, fetch: Callable[[str, dict[str, Any]], list[dict[str, Any]]]) -> None:
        self._fetch = fetch
        self.sql_log: list[str] = []
        self.param_log: list[dict[str, Any]] = []

    def fetchall(self, sql: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        assert_select(sql)
        params = parameters or {}
        self.sql_log.append(" ".join(sql.split()))
        self.param_log.append(dict(params))
        return self._fetch(sql, params)


def clamp_page_size(raw: int | None, default: int = 50) -> int:
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    if value not in (25, 50, 100):
        if value < 25:
            value = 25
        elif value <= 50:
            value = 50
        elif value <= 100:
            value = 100
        else:
            value = 100
    return value


def clamp_page(raw: int | None) -> int:
    try:
        value = int(raw or 0)
    except (TypeError, ValueError):
        value = 0
    return max(0, value)


def _where_clause(
    *,
    start_utc: Any | None,
    end_utc: Any | None,
) -> tuple[str, dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if start_utc is not None:
        clauses.append("decision_time >= {start_utc:DateTime64(3, 'UTC')}")
        params["start_utc"] = start_utc
    if end_utc is not None:
        clauses.append("decision_time <= {end_utc:DateTime64(3, 'UTC')}")
        params["end_utc"] = end_utc
    if clauses:
        return " WHERE " + " AND ".join(clauses), params
    return "", params


def count_signals(
    executor: SelectOnlyExecutor,
    side: Side,
    *,
    start_utc: Any | None = None,
    end_utc: Any | None = None,
) -> int:
    table = table_for_side(side)
    where_sql, params = _where_clause(start_utc=start_utc, end_utc=end_utc)
    sql = f"SELECT count() AS cnt FROM {table}{where_sql}"
    rows = executor.fetchall(sql, params)
    if not rows:
        return 0
    return int(rows[0].get("cnt") or 0)


def fetch_signal_rows(
    executor: SelectOnlyExecutor,
    side: Side,
    *,
    start_utc: Any | None = None,
    end_utc: Any | None = None,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    table = table_for_side(side)
    where_sql, params = _where_clause(start_utc=start_utc, end_utc=end_utc)
    params["limit"] = int(limit)
    params["offset"] = int(offset)
    sql = f"""
        SELECT
            decision_time,
            end_time,
            tracking_status,
            symbol,
            allowed,
            entry_price,
            initial_sl,
            tp,
            pnl_pct
        FROM {table}
        {where_sql}
        ORDER BY decision_time DESC
        LIMIT {{limit:UInt32}} OFFSET {{offset:UInt32}}
    """
    return executor.fetchall(sql, params)


def fetch_summary_metrics(
    executor: SelectOnlyExecutor,
    side: Side,
    *,
    start_utc: Any | None = None,
    end_utc: Any | None = None,
) -> dict[str, Any]:
    table = table_for_side(side)
    where_sql, params = _where_clause(start_utc=start_utc, end_utc=end_utc)
    sql = f"""
        SELECT
            count() AS total_signals,
            countIf(tracking_status != 'OPEN') AS closed_signals,
            countIf(tracking_status = 'OPEN') AS open_signals,
            countIf(
                allowed = 1
                AND tracking_status != 'OPEN'
                AND pnl_pct IS NOT NULL
                AND pnl_pct > 0
            ) AS winning_allowed,
            countIf(
                allowed = 1
                AND tracking_status != 'OPEN'
                AND pnl_pct IS NOT NULL
            ) AS allowed_closed_with_pnl,
            sumIf(
                pnl_pct,
                allowed = 1
                AND tracking_status != 'OPEN'
                AND pnl_pct IS NOT NULL
            ) AS total_pnl_sum
        FROM {table}
        {where_sql}
    """
    rows = executor.fetchall(sql, params)
    if not rows:
        return {
            "total_signals": 0,
            "closed_signals": 0,
            "open_signals": 0,
            "winning_allowed": 0,
            "allowed_closed_with_pnl": 0,
            "total_pnl_sum": 0.0,
        }
    row = rows[0]
    total_pnl = row.get("total_pnl_sum")
    return {
        "total_signals": int(row.get("total_signals") or 0),
        "closed_signals": int(row.get("closed_signals") or 0),
        "open_signals": int(row.get("open_signals") or 0),
        "winning_allowed": int(row.get("winning_allowed") or 0),
        "allowed_closed_with_pnl": int(row.get("allowed_closed_with_pnl") or 0),
        "total_pnl_sum": float(total_pnl) if total_pnl is not None else 0.0,
    }

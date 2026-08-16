"""SELECT-only Gold Shadow queries. Parameterized. No commits."""

from __future__ import annotations

import re
from typing import Any, Callable, Sequence

_WRITE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|REPLACE|GRANT|REVOKE|COMMIT|START\s+TRANSACTION)\b",
    re.IGNORECASE,
)


def assert_select(sql: str) -> str:
    cleaned = " ".join(sql.split())
    if not cleaned.upper().startswith("SELECT"):
        raise RuntimeError("gold shadow queries must start with SELECT")
    if _WRITE.search(cleaned):
        raise RuntimeError("gold shadow queries may not contain write DDL/DML")
    return sql


class SelectOnlyExecutor:
    def __init__(self, fetch: Callable[[str, Sequence[Any]], list[dict[str, Any]]]) -> None:
        self._fetch = fetch
        self.sql_log: list[str] = []

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        assert_select(sql)
        self.sql_log.append(" ".join(sql.split()))
        return self._fetch(sql, params)


def clamp_limit(raw: int | None, default: int, maximum: int) -> int:
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, maximum))


def clamp_offset(raw: int | None) -> int:
    try:
        value = int(raw or 0)
    except (TypeError, ValueError):
        value = 0
    return max(0, value)

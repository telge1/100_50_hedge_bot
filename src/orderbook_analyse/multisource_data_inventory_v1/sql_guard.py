"""Strict read-only SQL guard for audit queries."""

from __future__ import annotations

import re
from typing import Any

FORBIDDEN = re.compile(
    r"\b(INSERT|ALTER|DROP|DELETE|TRUNCATE|OPTIMIZE|CREATE|RENAME|SYSTEM|ATTACH|DETACH|KILL)\b",
    re.IGNORECASE,
)


class AuditQueryError(RuntimeError):
    pass


def assert_readonly_sql(sql: str) -> None:
    s = sql.lstrip()
    if not s.upper().startswith(("SELECT", "WITH")):
        raise AuditQueryError("only SELECT/WITH allowed")
    if FORBIDDEN.search(s):
        raise AuditQueryError("forbidden SQL token detected")


class ReadOnlyDB:
    """Thin wrapper around connect_readonly()."""

    def __init__(self, db: Any) -> None:
        self._db = db

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> Any:
        assert_readonly_sql(sql)
        return self._db.query(sql, parameters=parameters)

    def close(self) -> None:
        self._db.close()


def open_db() -> ReadOnlyDB:
    from orderbook_analyse.dynamic_wall_detector import connect_readonly

    return ReadOnlyDB(connect_readonly())

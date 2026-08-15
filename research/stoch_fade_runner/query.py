"""SELECT-only ClickHouse wrapper. insert/command are blocked."""

from __future__ import annotations

from typing import Any

from .candles import MutatingMethodBlocked


class ReadOnlyQueryClient:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.database = getattr(inner, "database", "signal_generator")

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> Any:
        token = sql.strip().split(None, 1)[0].upper()
        if token not in {"SELECT", "WITH"}:
            raise MutatingMethodBlocked(f"query:{token}")
        lowered = sql.lower()
        for banned in (" insert ", " alter ", " delete ", " truncate ", " drop "):
            if banned in f" {lowered} ":
                raise MutatingMethodBlocked("mutating_sql")
        return self._inner.query(sql, parameters=parameters)

    def insert(self, *args: Any, **kwargs: Any) -> None:
        raise MutatingMethodBlocked("insert")

    def command(self, *args: Any, **kwargs: Any) -> None:
        raise MutatingMethodBlocked("command")

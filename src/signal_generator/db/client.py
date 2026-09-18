"""Thin ClickHouse client wrapper (clickhouse-connect HTTP)."""

from __future__ import annotations

import threading
from typing import Any, Sequence

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from signal_generator.config import ClickHouseSettings, get_clickhouse_settings


class ClickHouseClient:
    """Small wrapper around clickhouse-connect focused on inserts + queries.

    clickhouse-connect sessions are not safe for concurrent queries. This wrapper
    serializes command/query/insert/ping on a per-instance RLock so callers that
    share one client across asyncio.to_thread / worker threads do not trip
    ``ProgrammingError: concurrent queries within the same session``.

    Parallel writers still need separate ``ClickHouseClient`` instances (e.g.
    candle path vs live public-trade inserts).
    """

    def __init__(self, client: Client, *, database: str) -> None:
        self._client = client
        self.database = database
        self._lock = threading.RLock()

    @classmethod
    def from_settings(cls, settings: ClickHouseSettings) -> ClickHouseClient:
        client = clickhouse_connect.get_client(
            host=settings.host,
            port=settings.port,
            username=settings.user,
            password=settings.password,
            database=settings.database,
        )
        return cls(client, database=settings.database)

    @property
    def raw(self) -> Client:
        return self._client

    def command(self, sql: str, parameters: dict[str, Any] | None = None) -> Any:
        with self._lock:
            return self._client.command(sql, parameters=parameters)

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> Any:
        with self._lock:
            return self._client.query(sql, parameters=parameters)

    def insert(
        self,
        table: str,
        data: Sequence[Sequence[Any]],
        column_names: Sequence[str],
        *,
        database: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> Any:
        if not data:
            return None
        kwargs: dict[str, Any] = {
            "table": table,
            "data": list(data),
            "column_names": list(column_names),
            "database": database or self.database,
        }
        if settings:
            kwargs["settings"] = settings
        with self._lock:
            return self._client.insert(**kwargs)

    def ping(self) -> tuple[str, str, str]:
        with self._lock:
            result = self._client.query(
                "SELECT version(), currentDatabase(), currentUser()"
            )
        row = result.result_rows[0]
        return str(row[0]), str(row[1]), str(row[2])

    def close(self) -> None:
        with self._lock:
            self._client.close()

    def __enter__(self) -> ClickHouseClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def get_client(*, settings: ClickHouseSettings | None = None) -> ClickHouseClient:
    if settings is None:
        settings = get_clickhouse_settings()
    return ClickHouseClient.from_settings(settings)

"""ClickHouse read client. SELECT only."""

from __future__ import annotations

from typing import Any

import clickhouse_connect

from shadow_signals.config import load_shadow_clickhouse_config
from shadow_signals.queries import SelectOnlyExecutor


def clickhouse_executor() -> SelectOnlyExecutor | None:
    try:
        load_shadow_clickhouse_config()
    except Exception:
        return None

    def fetch(sql: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        cfg = load_shadow_clickhouse_config()
        client = clickhouse_connect.get_client(**cfg.connect_kwargs())
        try:
            result = client.query(sql, parameters=parameters)
            columns = list(result.column_names or [])
            out: list[dict[str, Any]] = []
            for row in result.result_rows or []:
                out.append({columns[i]: row[i] for i in range(len(columns))})
            return out
        finally:
            client.close()

    return SelectOnlyExecutor(fetch)


def configured_executor() -> SelectOnlyExecutor | None:
    return clickhouse_executor()

"""Thin ClickHouse access via clickhouse_connect (project standard)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


def load_ch_env() -> None:
    oa = Path("/home/telgenbuescher/projects/orderbook_analyse")
    load_dotenv(oa / ".env")


def get_ch_client(database: str | None = None):
    import clickhouse_connect

    load_ch_env()
    kwargs: dict[str, Any] = {
        "host": os.environ.get("CLICKHOUSE_HOST", "localhost"),
        "port": int(os.environ.get("CLICKHOUSE_HTTP_PORT") or 8123),
        "username": os.environ.get("CLICKHOUSE_USER", "default"),
        "password": os.environ.get("CLICKHOUSE_PASSWORD", ""),
    }
    if database:
        kwargs["database"] = database
    return clickhouse_connect.get_client(**kwargs)


def ch_command(client, sql: str) -> None:
    client.command(sql)


def ch_query(client, sql: str, parameters: dict | None = None) -> list[tuple]:
    r = client.query(sql, parameters=parameters or {})
    return list(r.result_rows)

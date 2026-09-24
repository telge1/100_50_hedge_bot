"""Pymysql SELECT adapter. autocommit True, never commit()."""

from __future__ import annotations

from typing import Any, Sequence

from .config import GoldShadowDbConfig, load_gold_shadow_db_config
from .queries import SelectOnlyExecutor, assert_select


def mysql_executor(config: GoldShadowDbConfig) -> SelectOnlyExecutor:
    import pymysql
    from pymysql.cursors import DictCursor

    def fetch(sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
        assert_select(sql)
        conn = pymysql.connect(**config.connect_kwargs(), cursorclass=DictCursor)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = list(cur.fetchall() or [])
            return rows
        finally:
            conn.close()

    return SelectOnlyExecutor(fetch)


def configured_executor(environ: dict[str, str] | None = None) -> SelectOnlyExecutor | None:
    config = load_gold_shadow_db_config(environ)
    if config is None:
        return None
    return mysql_executor(config)

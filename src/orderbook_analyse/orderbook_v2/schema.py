"""Apply _v2 schema to ClickHouse."""
from __future__ import annotations

import re
from pathlib import Path


def _strip_sql_comments(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", "", sql)
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    return sql


def apply_schema(client: object, sql_path: Path | None = None) -> list[str]:
    """Execute CREATE TABLE IF NOT EXISTS statements from 004_orderbook_v2_schema.sql."""
    if sql_path is None:
        # __file__ is src/orderbook_analyse/orderbook_v2/schema.py -> parents[3] = project root
        sql_path = Path(__file__).parents[3] / "sql" / "004_orderbook_v2_schema.sql"
    raw = sql_path.read_text(encoding="utf-8")
    cleaned = _strip_sql_comments(raw)
    stmts = [s.strip() for s in cleaned.split(";") if s.strip()]
    errors: list[str] = []
    for stmt in stmts:
        if not stmt.upper().startswith("CREATE"):
            continue
        try:
            client.command(stmt)  # type: ignore[attr-defined]
        except Exception as e:
            errors.append(f"{stmt[:60]!r}: {e}")
    return errors

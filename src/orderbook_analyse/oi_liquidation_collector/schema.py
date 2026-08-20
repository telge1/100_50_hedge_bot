"""Apply collector DDL. Never touches orderbook/public_trades/ticker_samples."""

from __future__ import annotations

from pathlib import Path

from .settings import PROJECT_ROOT

SCHEMA_SQL = PROJECT_ROOT / "sql" / "003_oi_liquidation_schema.sql"
FORBIDDEN_SQL_TOKENS = (
    "orderbook_deltas",
    "public_trades",
    "ticker_samples",
    "candles_1m",
)


def _strip_sql_comments(sql: str) -> str:
    lines: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        if "--" in line:
            line = line[: line.index("--")]
        if line.strip():
            lines.append(line)
    return "\n".join(lines)


def apply_schema(client) -> None:
    sql = _strip_sql_comments(SCHEMA_SQL.read_text())
    statements = [s.strip() for s in sql.split(";") if s.strip()]
    creates = []
    for body in statements:
        if not body.upper().startswith("CREATE TABLE"):
            raise RuntimeError(f"schema contains non-create statement: {body[:80]}")
        creates.append(body)
        for token in FORBIDDEN_SQL_TOKENS:
            if token in body:
                raise RuntimeError(f"schema create mentions forbidden table {token}")
    for stmt in creates:
        client.command(stmt)

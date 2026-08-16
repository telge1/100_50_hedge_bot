"""Idempotent ClickHouse schema setup (no DROP / TRUNCATE)."""

from __future__ import annotations

from pathlib import Path

import clickhouse_connect

from signal_generator.config import ClickHouseSettings, get_clickhouse_settings
from signal_generator.db.client import ClickHouseClient

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"


def _split_sql_statements(sql: str) -> list[str]:
    """Split a migration file into executable statements (simple ; splitter)."""
    statements: list[str] = []
    buf: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        buf.append(line)
        if stripped.endswith(";"):
            stmt = "\n".join(buf).strip().rstrip(";").strip()
            if stmt:
                statements.append(stmt)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def apply_migrations(
    client: ClickHouseClient,
    *,
    migrations_dir: Path | None = None,
) -> list[str]:
    """Apply all ``*.sql`` migrations in sorted order. Returns executed statements."""
    directory = migrations_dir or MIGRATIONS_DIR
    executed: list[str] = []
    for path in sorted(directory.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        for statement in _split_sql_statements(text):
            client.command(statement)
            executed.append(statement)
    return executed


def setup_clickhouse(
    *,
    settings: ClickHouseSettings | None = None,
    migrations_dir: Path | None = None,
) -> ClickHouseClient:
    """Create database + tables if needed. Safe to run repeatedly.

    Connects first without requiring the target database to exist, creates it
    via migrations, then returns a client bound to the target database.
    """
    settings = settings or get_clickhouse_settings()

    # Bootstrap client on default DB so CREATE DATABASE works even if target is new.
    bootstrap = clickhouse_connect.get_client(
        host=settings.host,
        port=settings.port,
        username=settings.user,
        password=settings.password,
        database="default",
    )
    bootstrap_client = ClickHouseClient(bootstrap, database="default")
    try:
        apply_migrations(bootstrap_client, migrations_dir=migrations_dir)
    finally:
        bootstrap_client.close()

    return ClickHouseClient.from_settings(settings)


def table_exists(client: ClickHouseClient, table: str) -> bool:
    result = client.query(
        """
        SELECT count()
        FROM system.tables
        WHERE database = {db:String} AND name = {name:String}
        """,
        parameters={"db": client.database, "name": table},
    )
    return int(result.result_rows[0][0]) > 0

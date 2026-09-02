"""ClickHouse access with fail-closed write-target validation."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

from .contracts import TARGET_DATABASE, assert_target_database

ROOT = Path(__file__).resolve().parents[2]
_WRITE_RE = re.compile(
    r"^\s*(?:CREATE\s+(?:DATABASE|TABLE)|INSERT\s+INTO)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?(?P<target>[`\w.]+)",
    re.IGNORECASE,
)
_FORBIDDEN_RE = re.compile(
    r"\b(?:DROP|TRUNCATE|OPTIMIZE|ALTER|DELETE|SYSTEM|RENAME)\b", re.IGNORECASE
)


def connect():
    dashboard = str(ROOT / "dashboard")
    if dashboard not in sys.path:
        sys.path.insert(0, dashboard)
    import clickhouse_connect
    from research_charts.clickhouse_config import load_clickhouse_config

    cfg = load_clickhouse_config()
    return clickhouse_connect.get_client(**cfg.connect_kwargs())


def validate_write_sql(sql: str) -> None:
    if _FORBIDDEN_RE.search(sql):
        raise PermissionError("forbidden ClickHouse write statement")
    match = _WRITE_RE.search(sql)
    if not match:
        raise PermissionError("unrecognized write statement")
    target = match.group("target").replace("`", "")
    database = target.split(".", 1)[0]
    assert_target_database(database)


def execute_ddl(client: Any, sql: str) -> None:
    validate_write_sql(sql)
    client.command(sql)


def table_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"invalid table name: {name!r}")
    assert_target_database(TARGET_DATABASE)
    return f"{TARGET_DATABASE}.{name}"


def insert(
    client: Any,
    name: str,
    rows: Iterable[Sequence[Any]],
    columns: Sequence[str],
) -> None:
    target = table_name(name)
    materialized = list(rows)
    if materialized:
        client.insert(target, materialized, column_names=list(columns))


def scalar(client: Any, sql: str, parameters: dict[str, Any] | None = None) -> Any:
    return client.query(sql, parameters=parameters or {}).result_rows[0][0]


def rows(client: Any, sql: str, parameters: dict[str, Any] | None = None) -> list[tuple]:
    return client.query(sql, parameters=parameters or {}).result_rows


def _text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8").rstrip("\x00")
    return str(value).rstrip("\x00")


def ensure_batch_available(
    client: Any, batch_id: str, input_fingerprint: str
) -> bool:
    found = rows(
        client,
        f"SELECT input_fingerprint, status FROM {TARGET_DATABASE}.research_ingestion_batches "
        "WHERE batch_id = %(batch_id)s",
        {"batch_id": batch_id},
    )
    if not found:
        return True
    fingerprints = {_text(row[0]) for row in found}
    if fingerprints != {input_fingerprint}:
        raise RuntimeError(f"CONFLICT batch_id={batch_id}")
    if any(str(row[1]) != "COMPLETE" for row in found):
        raise RuntimeError(f"incomplete existing batch_id={batch_id}")
    return False


def ensure_source_file_available(
    client: Any, source_file_id: str, source_fingerprint: str
) -> bool:
    found = rows(
        client,
        f"SELECT source_fingerprint FROM {TARGET_DATABASE}.research_source_files "
        "WHERE source_file_id = %(source_file_id)s",
        {"source_file_id": source_file_id},
    )
    if not found:
        return True
    if {_text(row[0]) for row in found} != {source_fingerprint}:
        raise RuntimeError(f"CONFLICT source_file_id={source_file_id}")
    return False

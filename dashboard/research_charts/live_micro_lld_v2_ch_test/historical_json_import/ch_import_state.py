"""Read-only ClickHouse snapshot / publish state for controlled import orchestration."""

from __future__ import annotations

from typing import Any

import clickhouse_connect

from ...clickhouse_config import load_clickhouse_config


def ch_client_and_db() -> tuple[Any, str]:
    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    return client, cfg.database


def fetch_published_snapshot_ids(
    client: Any,
    database: str,
    *,
    pipeline_line_id: str,
) -> set[str]:
    q = f"""
    SELECT DISTINCT snapshot_id
    FROM {database}.research_lmv2_active_snapshot_publish_v1
    WHERE pipeline_line_id = {{line:String}}
    """
    return {
        str(r[0])
        for r in client.query(q, parameters={"line": pipeline_line_id}).result_rows
    }


def fetch_meta_complete_snapshot_ids(client: Any, database: str) -> set[str]:
    q = f"""
    SELECT snapshot_id
    FROM {database}.research_lmv2_active_snapshot_meta_v1
    WHERE write_complete = 1
    """
    return {str(r[0]) for r in client.query(q).result_rows}


def lmv2_table_row_counts(client: Any, database: str) -> dict[str, int]:
    tables = (
        "research_lmv2_active_snapshot_meta_v1",
        "research_lmv2_active_snapshot_member_v1",
        "research_lmv2_snapshot_active_pool_v1",
        "research_lmv2_active_snapshot_publish_v1",
        "research_lmv2_snapshot_import_provenance_v1",
    )
    out: dict[str, int] = {}
    for table in tables:
        n = int(
            client.query(f"SELECT count() FROM {database}.{table}").result_rows[0][0]
        )
        out[table] = n
    return out


def parts_and_merges_summary(client: Any, database: str) -> dict[str, Any]:
    parts_q = """
    SELECT
        table,
        sum(rows) AS rows,
        sum(bytes_on_disk) AS bytes_on_disk,
        count() AS part_count,
        sum(if(active, 1, 0)) AS active_parts
    FROM system.parts
    WHERE database = {db:String}
      AND table IN (
        'research_lmv2_active_snapshot_meta_v1',
        'research_lmv2_active_snapshot_member_v1',
        'research_lmv2_snapshot_active_pool_v1',
        'research_lmv2_active_snapshot_publish_v1',
        'research_lmv2_snapshot_import_provenance_v1'
      )
      AND active
    GROUP BY table
    ORDER BY table
    """
    parts_rows = client.query(parts_q, parameters={"db": database}).result_rows
    merge_q = """
    SELECT count()
    FROM system.merges
    WHERE database = {db:String}
      AND table LIKE 'research_lmv2_%'
    """
    merging = int(client.query(merge_q, parameters={"db": database}).result_rows[0][0])
    return {
        "parts_by_table": [
            {
                "table": str(r[0]),
                "rows": int(r[1]),
                "bytes_on_disk": int(r[2]),
                "part_count": int(r[3]),
                "active_parts": int(r[4]),
            }
            for r in parts_rows
        ],
        "active_merges_lmv2": merging,
    }


def read_ch_import_preflight(
    *,
    pipeline_line_id: str,
    scope_snapshot_ids: set[str] | None = None,
) -> dict[str, Any]:
    """
    Read-only CH state before/after a controlled run (no writes).

    ``scope_snapshot_ids``: optional subset for publish overlap checks on the approved line.
    """
    client, db = ch_client_and_db()
    try:
        published_line = fetch_published_snapshot_ids(
            client, db, pipeline_line_id=pipeline_line_id
        )
        meta_complete = fetch_meta_complete_snapshot_ids(client, db)
        overlap: list[str] = []
        if scope_snapshot_ids:
            overlap = sorted(scope_snapshot_ids & published_line)
        from .orphan_audit import audit_orphan_members, mutation_status_member_delete

        return {
            "table_row_counts": lmv2_table_row_counts(client, db),
            "parts_and_merges": parts_and_merges_summary(client, db),
            "published_on_line_count": len(published_line),
            "meta_complete_count": len(meta_complete),
            "scope_already_published_on_line": overlap,
            "orphan_member_snapshots": audit_orphan_members(client, db),
            "recent_member_mutations": mutation_status_member_delete(client, db),
        }
    finally:
        client.close()

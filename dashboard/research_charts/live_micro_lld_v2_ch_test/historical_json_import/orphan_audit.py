"""Read-only ClickHouse orphan / partial snapshot audits."""

from __future__ import annotations

from typing import Any

import clickhouse_connect

from ...clickhouse_config import load_clickhouse_config


def audit_orphan_members(client: Any, database: str) -> list[dict[str, Any]]:
    """Members without complete meta (write_complete=1)."""
    q = f"""
    SELECT m.snapshot_id, count() AS member_rows
    FROM {database}.research_lmv2_active_snapshot_member_v1 AS m
    LEFT JOIN (
        SELECT snapshot_id FROM {database}.research_lmv2_active_snapshot_meta_v1
        WHERE write_complete = 1
    ) AS meta ON m.snapshot_id = meta.snapshot_id
    WHERE meta.snapshot_id IS NULL OR meta.snapshot_id = ''
    GROUP BY m.snapshot_id
    ORDER BY member_rows DESC
    LIMIT 100
    """
    return [
        {"snapshot_id": str(r[0]), "member_rows": int(r[1])}
        for r in client.query(q).result_rows
    ]


def audit_snapshot_layers(
    client: Any,
    database: str,
    snapshot_id: str,
) -> dict[str, int]:
    tables = {
        "meta_complete": f"""
            SELECT count() FROM {database}.research_lmv2_active_snapshot_meta_v1
            WHERE snapshot_id = {{sid:String}} AND write_complete = 1
        """,
        "members": f"""
            SELECT count() FROM {database}.research_lmv2_active_snapshot_member_v1
            WHERE snapshot_id = {{sid:String}}
        """,
        "catalog": f"""
            SELECT count() FROM {database}.research_lmv2_snapshot_active_pool_v1
            WHERE snapshot_id = {{sid:String}}
        """,
        "publish_any": f"""
            SELECT count() FROM {database}.research_lmv2_active_snapshot_publish_v1
            WHERE snapshot_id = {{sid:String}}
        """,
        "sidecar_any": f"""
            SELECT count() FROM {database}.research_lmv2_snapshot_import_provenance_v1
            WHERE snapshot_id = {{sid:String}}
        """,
    }
    out: dict[str, int] = {}
    for key, q in tables.items():
        out[key] = int(
            client.query(q, parameters={"sid": snapshot_id}).result_rows[0][0]
        )
    return out


def mutation_status_member_delete(
    client: Any,
    database: str,
) -> list[dict[str, Any]]:
    q = """
    SELECT mutation_id, command, is_done, latest_fail_reason
    FROM system.mutations
    WHERE database = {db:String}
      AND table = 'research_lmv2_active_snapshot_member_v1'
    ORDER BY create_time DESC
    LIMIT 5
    """
    return [
        {
            "mutation_id": str(r[0]),
            "command": str(r[1]),
            "is_done": int(r[2]),
            "latest_fail_reason": str(r[3] or ""),
        }
        for r in client.query(q, parameters={"db": database}).result_rows
    ]


def run_orphan_audit(snapshot_id: str | None = None) -> dict[str, Any]:
    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database
    try:
        orphans = audit_orphan_members(client, db)
        mutations = mutation_status_member_delete(client, db)
        focus = None
        if snapshot_id:
            focus = audit_snapshot_layers(client, db, snapshot_id)
        return {
            "orphan_member_snapshots": orphans,
            "orphan_count": len(orphans),
            "recent_member_mutations": mutations,
            "focus_snapshot": focus,
        }
    finally:
        client.close()

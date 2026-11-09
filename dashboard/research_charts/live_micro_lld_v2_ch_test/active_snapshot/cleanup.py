"""Test-only repair for incomplete snapshot writes (resume after simulated crash)."""

from __future__ import annotations

import clickhouse_connect

from ...clickhouse_config import load_clickhouse_config
from .schema import TABLE_SNAPSHOT_MEMBER, ensure_active_snapshot_tables
from .writer import _meta_row_complete, snapshot_id_for_state
from .extract import ActiveStateSnapshot


def cleanup_orphan_members_for_snapshot(snapshot_id: str) -> int:
    """
    Remove member rows when meta is not write_complete=1.
    Used only by isolated crash/resume tests.
    """
    ensure_active_snapshot_tables()
    ch_cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**ch_cfg.connect_kwargs())
    db = ch_cfg.database
    try:
        meta = _meta_row_complete(client, db, snapshot_id)
        if meta and meta["write_complete"] == 1:
            return 0
        client.command(
            f"ALTER TABLE {db}.{TABLE_SNAPSHOT_MEMBER} DELETE WHERE snapshot_id = {{sid:String}}",
            parameters={"sid": snapshot_id},
        )
        return 1
    finally:
        client.close()


def cleanup_before_retry(state: ActiveStateSnapshot) -> None:
    sid = snapshot_id_for_state(state)
    cleanup_orphan_members_for_snapshot(sid)

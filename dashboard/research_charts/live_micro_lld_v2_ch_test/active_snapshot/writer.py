"""Write validated ACTIVE snapshots to isolated CH tables (idempotent, no partial reads)."""

from __future__ import annotations

import uuid
from typing import Any

import clickhouse_connect

from ...clickhouse_config import load_clickhouse_config
from .extract import ActiveStateSnapshot
from .fingerprint import deterministic_snapshot_id
from .schema import TABLE_SNAPSHOT_MEMBER, TABLE_SNAPSHOT_META, ensure_active_snapshot_tables


class SnapshotWriteError(Exception):
    pass


class PartialSnapshotError(SnapshotWriteError):
    pass


class SnapshotExistsError(SnapshotWriteError):
    pass


def snapshot_id_for_state(state: ActiveStateSnapshot) -> str:
    return deterministic_snapshot_id(
        symbol=state.symbol,
        timeframe=state.timeframe,
        pool_detail=state.pool_detail,
        config_hash=state.config_hash,
        candle_load_start=state.candle_load_start,
        decision_close=state.decision_close,
        prefix_candle_fingerprint=state.prefix_candle_fingerprint,
        engine_version=state.engine_version,
    )


def _client():
    ch_cfg = load_clickhouse_config()
    return ch_cfg, clickhouse_connect.get_client(**ch_cfg.connect_kwargs())


def _meta_row_complete(client, db: str, snapshot_id: str) -> dict[str, Any] | None:
    q = f"""
    SELECT snapshot_id, write_complete, active_pool_count, active_state_fingerprint
    FROM {db}.{TABLE_SNAPSHOT_META} FINAL
    WHERE snapshot_id = {{sid:String}}
    LIMIT 1
    """
    rows = client.query(q, parameters={"sid": snapshot_id}).result_rows
    if not rows:
        return None
    return {
        "snapshot_id": rows[0][0],
        "write_complete": int(rows[0][1]),
        "active_pool_count": int(rows[0][2]),
        "active_state_fingerprint": rows[0][3],
    }


def _member_count(client, db: str, snapshot_id: str) -> int:
    q = f"""
    SELECT count() FROM {db}.{TABLE_SNAPSHOT_MEMBER}
    WHERE snapshot_id = {{sid:String}}
    """
    return int(client.query(q, parameters={"sid": snapshot_id}).result_rows[0][0])


def write_active_snapshot(
    state: ActiveStateSnapshot,
    *,
    test_run_id: str,
    allow_existing_complete: bool = True,
) -> dict[str, Any]:
    """
    Two-phase write: members first, then meta with write_complete=1.
    Idempotent if an identical complete snapshot already exists.
    """
    ensure_active_snapshot_tables()
    ch_cfg, client = _client()
    db = ch_cfg.database
    sid = snapshot_id_for_state(state)
    try:
        existing = _meta_row_complete(client, db, sid)
        if existing:
            if existing["write_complete"] != 1:
                mc = _member_count(client, db, sid)
                raise PartialSnapshotError(
                    f"incomplete snapshot {sid}: meta write_complete=0, members={mc}"
                )
            if allow_existing_complete:
                return {
                    "snapshot_id": sid,
                    "test_run_id": test_run_id,
                    "status": "skipped_existing",
                    "write_complete": 1,
                }
            raise SnapshotExistsError(f"snapshot {sid} already exists")

        partial_members = _member_count(client, db, sid)
        if partial_members > 0:
            raise PartialSnapshotError(
                f"orphan members for {sid} without complete meta: count={partial_members}"
            )

        member_rows = [
            (sid, pid, side, idx)
            for idx, (pid, side) in enumerate(
                zip(state.active_pool_ids, state.active_sides)
            )
        ]
        if member_rows:
            client.insert(
                f"{db}.{TABLE_SNAPSHOT_MEMBER}",
                member_rows,
                column_names=["snapshot_id", "pool_id", "side", "sort_index"],
            )

        meta_row = [
            (
                sid,
                uuid.UUID(test_run_id),
                state.symbol,
                state.timeframe,
                state.pool_detail,
                state.config_hash,
                state.engine_version,
                state.engine_input_hash,
                state.candle_load_start,
                state.history_from_close,
                state.decision_close,
                state.prefix_candle_count,
                state.prefix_candle_fingerprint,
                state.known_pool_count,
                state.active_pool_count,
                state.active_upper_count,
                state.active_lower_count,
                state.active_state_fingerprint,
                1,
            )
        ]
        client.insert(
            f"{db}.{TABLE_SNAPSHOT_META}",
            meta_row,
            column_names=[
                "snapshot_id",
                "test_run_id",
                "symbol",
                "timeframe",
                "pool_detail",
                "config_hash",
                "engine_version",
                "engine_input_hash",
                "candle_load_start",
                "history_from_close",
                "decision_close",
                "prefix_candle_count",
                "prefix_candle_fingerprint",
                "known_pool_count",
                "active_pool_count",
                "active_upper_count",
                "active_lower_count",
                "active_state_fingerprint",
                "write_complete",
            ],
        )
        return {
            "snapshot_id": sid,
            "test_run_id": test_run_id,
            "status": "written",
            "write_complete": 1,
            "active_pool_count": state.active_pool_count,
        }
    finally:
        client.close()

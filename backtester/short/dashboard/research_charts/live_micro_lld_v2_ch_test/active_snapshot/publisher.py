"""Publish validated snapshots to the isolated publish ledger."""

from __future__ import annotations

from typing import Any

import clickhouse_connect

from ...clickhouse_config import load_clickhouse_config
from .extract import ActiveStateSnapshot
from .faults import FaultInjector
from .publish_guard import (
    LineIdentity,
    PublishConflictError,
    PublishGuardError,
    deterministic_publish_id,
    validate_snapshot_against_state,
)
from .publish_schema import TABLE_PUBLISH, ensure_publish_tables
from .reader import ActiveSnapshotReader
from .staged_writer import write_active_snapshot_staged
from .writer import snapshot_id_for_state


def _client():
    ch_cfg = load_clickhouse_config()
    return ch_cfg, clickhouse_connect.get_client(**ch_cfg.connect_kwargs())


def _published_row(
    client,
    db: str,
    pipeline_line_id: str,
    decision_close,
    config_hash: str,
) -> dict[str, Any] | None:
    q = f"""
    SELECT publish_id, snapshot_id, active_state_fingerprint, prefix_candle_fingerprint,
           publish_complete
    FROM {db}.{TABLE_PUBLISH} FINAL
    WHERE pipeline_line_id = {{lid:String}}
      AND config_hash = {{ch:String}}
      AND decision_close = {{dc:DateTime64(3, 'UTC')}}
    LIMIT 1
    """
    rows = client.query(
        q,
        parameters={
            "lid": pipeline_line_id,
            "ch": config_hash,
            "dc": decision_close,
        },
    ).result_rows
    if not rows:
        return None
    return {
        "publish_id": rows[0][0],
        "snapshot_id": rows[0][1],
        "active_state_fingerprint": rows[0][2],
        "prefix_candle_fingerprint": rows[0][3],
        "publish_complete": int(rows[0][4]),
    }


def publish_validated_snapshot(
    state: ActiveStateSnapshot,
    *,
    pipeline_line_id: str,
    test_run_id: str,
    line: LineIdentity,
    reader: ActiveSnapshotReader | None = None,
    faults: FaultInjector | None = None,
    allow_existing_publish: bool = True,
) -> dict[str, Any]:
    """
    computed → write → validated (guard) → published.
    """
    faults = faults or FaultInjector()
    ensure_publish_tables()
    reader = reader or ActiveSnapshotReader()
    sid = snapshot_id_for_state(state)
    pub_id = deterministic_publish_id(sid)

    ch_cfg, client = _client()
    db = ch_cfg.database
    try:
        existing = _published_row(
            client, db, pipeline_line_id, state.decision_close, state.config_hash
        )
        if existing and existing["publish_complete"] == 1:
            if existing["snapshot_id"] != sid:
                raise PublishConflictError(
                    f"decision_close {state.decision_close} already published with "
                    f"snapshot {existing['snapshot_id']}, new {sid}"
                )
            if existing["prefix_candle_fingerprint"] != state.prefix_candle_fingerprint:
                raise PublishConflictError("input fingerprint conflict for decision_close")
            if allow_existing_publish:
                return {
                    "status": "skipped_existing_publish",
                    "publish_id": existing["publish_id"],
                    "snapshot_id": existing["snapshot_id"],
                }

        write_active_snapshot_staged(
            state,
            test_run_id=test_run_id,
            faults=faults,
        )
        validation = validate_snapshot_against_state(reader, sid, state, line)
        faults.maybe_raise("before_publish_insert")

        row = [
            (
                pub_id,
                pipeline_line_id,
                sid,
                state.symbol,
                state.timeframe,
                state.pool_detail,
                state.config_hash,
                state.engine_version,
                state.engine_input_hash,
                state.candle_load_start,
                state.history_from_close,
                state.decision_close,
                state.prefix_candle_fingerprint,
                state.active_state_fingerprint,
                1,
                state.history_policy or "",
            )
        ]
        client.insert(
            f"{db}.{TABLE_PUBLISH}",
            row,
            column_names=[
                "publish_id",
                "pipeline_line_id",
                "snapshot_id",
                "symbol",
                "timeframe",
                "pool_detail",
                "config_hash",
                "engine_version",
                "engine_input_hash",
                "candle_load_start",
                "history_from_close",
                "decision_close",
                "prefix_candle_fingerprint",
                "active_state_fingerprint",
                "publish_complete",
                "history_policy",
            ],
        )
        faults.maybe_raise("after_publish")
        return {
            "status": "published",
            "publish_id": pub_id,
            "snapshot_id": sid,
            "validation": validation,
        }
    finally:
        client.close()

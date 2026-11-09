"""Insert active pool catalog rows from JSON-derived dicts (post-approval only)."""

from __future__ import annotations

import os
from typing import Any, Sequence

import clickhouse_connect

from ..active_snapshot.pool_catalog_schema import TABLE_ACTIVE_POOL, ensure_pool_catalog_table
from ...clickhouse_config import load_clickhouse_config
from .catalog_integrity import CatalogIdentityError, validate_catalog_rows_in_memory

_WRITE_ENV = "HISTORICAL_JSON_IMPORT_ALLOW_WRITE"


class CatalogIncompleteError(Exception):
    """Catalog row count does not match expected ACTIVE membership."""


def catalog_row_count(snapshot_id: str) -> int:
    ch_cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**ch_cfg.connect_kwargs())
    db = ch_cfg.database
    try:
        return int(
            client.query(
                f"SELECT count() FROM {db}.{TABLE_ACTIVE_POOL} WHERE snapshot_id = {{sid:String}}",
                parameters={"sid": snapshot_id},
            ).result_rows[0][0]
        )
    finally:
        client.close()


def write_active_pool_catalog_from_json_rows(
    catalog_rows: Sequence[dict[str, Any]],
    *,
    snapshot_id: str,
    expected_count: int | None = None,
    state: Any | None = None,
) -> dict[str, Any]:
    """
    Same insert shape as ``write_active_pool_catalog`` but without ``LiquidityPoolV2``.
    Gated by ``HISTORICAL_JSON_IMPORT_ALLOW_WRITE=1``.
    """
    if os.environ.get(_WRITE_ENV) != "1":
        raise RuntimeError(f"blocked: set {_WRITE_ENV}=1 before catalog insert")
    if not catalog_rows:
        return {"status": "skipped_empty", "count": 0}
    if state is not None:
        validate_catalog_rows_in_memory(catalog_rows, state)
    pids = [str(r["pool_id"]) for r in catalog_rows]
    if len(pids) != len(set(pids)):
        raise CatalogIdentityError("duplicate pool_id in catalog_rows batch")
    ensure_pool_catalog_table()
    ch_cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**ch_cfg.connect_kwargs())
    db = ch_cfg.database
    try:
        existing = client.query(
            f"SELECT count() FROM {db}.{TABLE_ACTIVE_POOL} WHERE snapshot_id = {{sid:String}}",
            parameters={"sid": snapshot_id},
        ).result_rows[0][0]
        if int(existing) > 0:
            exp = expected_count if expected_count is not None else len(catalog_rows)
            if int(existing) != exp:
                raise CatalogIncompleteError(
                    f"snapshot {snapshot_id}: partial or corrupt catalog "
                    f"({int(existing)} rows, expected {exp}); manual cleanup required before retry"
                )
            dup_q = f"""
            SELECT pool_id, count() AS c FROM {db}.{TABLE_ACTIVE_POOL}
            WHERE snapshot_id = {{sid:String}}
            GROUP BY pool_id HAVING c > 1
            LIMIT 1
            """
            dup = client.query(dup_q, parameters={"sid": snapshot_id}).result_rows
            if dup:
                raise CatalogIdentityError(
                    f"duplicate catalog rows for snapshot {snapshot_id} pool_id={dup[0][0]}"
                )
            return {"status": "skipped_existing", "count": int(existing)}

        insert_rows = [
            (
                r["snapshot_id"],
                r["pipeline_line_id"],
                r["symbol"],
                r["timeframe"],
                r["pool_detail"],
                r["decision_close"],
                r["history_policy"],
                r["pool_id"],
                r["pool_type"],
                r["side"],
                r["top_price"],
                r["bottom_price"],
                r["strength"],
                r["strength_class"],
                r["known_at"],
                r["break_at"],
                r["reclaim_level"],
                r["candidate_created_at"],
                r["confirmed_at"],
                r["invalidated_at"],
                r["created_index"],
                r["row_fingerprint"],
            )
            for r in catalog_rows
        ]
        client.insert(
            f"{db}.{TABLE_ACTIVE_POOL}",
            insert_rows,
            column_names=[
                "snapshot_id",
                "pipeline_line_id",
                "symbol",
                "timeframe",
                "pool_detail",
                "decision_close",
                "history_policy",
                "pool_id",
                "pool_type",
                "side",
                "top_price",
                "bottom_price",
                "strength",
                "strength_class",
                "known_at",
                "break_at",
                "reclaim_level",
                "candidate_created_at",
                "confirmed_at",
                "invalidated_at",
                "created_index",
                "row_fingerprint",
            ],
        )
        return {"status": "written", "count": len(insert_rows)}
    finally:
        client.close()

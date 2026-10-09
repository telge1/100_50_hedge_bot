"""Write/read active pool attributes for a snapshot."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

import clickhouse_connect

from ...clickhouse_config import load_clickhouse_config
from ..serialize import pool_fingerprint, row_fingerprint
from ..serialize import pool_to_ch_row
from indicators.live_micro_lld_v2.models import LiquidityPoolV2
from .extract import ActiveStateSnapshot
from .pool_catalog_schema import TABLE_ACTIVE_POOL, ensure_pool_catalog_table


def _fp_tuple_to_str(fp: tuple) -> str:
    return "|".join(str(x) for x in fp)


def write_active_pool_catalog(
    state: ActiveStateSnapshot,
    engine_pools: Sequence[LiquidityPoolV2],
    *,
    snapshot_id: str,
    pipeline_line_id: str,
) -> dict[str, Any]:
    ensure_pool_catalog_table()
    active_ids = set(state.active_pool_ids)
    rows = []
    for p in engine_pools:
        if p.pool_id not in active_ids:
            continue
        ch = pool_to_ch_row(p, run_id=snapshot_id)
        ch["row_fingerprint"] = _fp_tuple_to_str(pool_fingerprint(p))
        rows.append(ch)
    if len(rows) != len(active_ids):
        missing = active_ids - {r["pool_id"] for r in rows}
        raise ValueError(f"active pool catalog missing engine rows: {sorted(missing)[:5]}")

    ch_cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**ch_cfg.connect_kwargs())
    db = ch_cfg.database
    try:
        existing = client.query(
            f"SELECT count() FROM {db}.{TABLE_ACTIVE_POOL} WHERE snapshot_id = {{sid:String}}",
            parameters={"sid": snapshot_id},
        ).result_rows[0][0]
        if int(existing) > 0:
            return {"status": "skipped_existing", "count": int(existing)}

        insert_rows = [
            (
                snapshot_id,
                pipeline_line_id,
                state.symbol,
                state.timeframe,
                state.pool_detail,
                state.decision_close,
                state.history_policy,
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
            for r in rows
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


def load_active_pool_catalog(snapshot_id: str) -> list[dict[str, Any]]:
    ensure_pool_catalog_table()
    ch_cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**ch_cfg.connect_kwargs())
    db = ch_cfg.database
    try:
        q = f"""
        SELECT pool_id, side, pool_type, top_price, bottom_price, strength, strength_class,
               known_at, break_at, reclaim_level, row_fingerprint
        FROM {db}.{TABLE_ACTIVE_POOL}
        WHERE snapshot_id = {{sid:String}}
        ORDER BY pool_id
        """
        return [
            {
                "pool_id": r[0],
                "side": r[1],
                "pool_type": r[2],
                "top_price": r[3],
                "bottom_price": r[4],
                "strength": r[5],
                "strength_class": r[6],
                "known_at": r[7],
                "break_at": r[8],
                "reclaim_level": r[9],
                "row_fingerprint": r[10],
            }
            for r in client.query(q, parameters={"sid": snapshot_id}).result_rows
        ]
    finally:
        client.close()


def compare_catalog_to_engine(
    catalog: list[dict[str, Any]],
    engine_pools: Sequence[LiquidityPoolV2],
    active_ids: set[str],
) -> dict[str, Any]:
    by_id = {p.pool_id: p for p in engine_pools if p.pool_id in active_ids}
    cat_ids = {r["pool_id"] for r in catalog}
    missing = sorted(active_ids - cat_ids)
    extra = sorted(cat_ids - active_ids)
    fp_mismatch = []
    for r in catalog:
        p = by_id.get(r["pool_id"])
        if not p:
            continue
        eng_fp = _fp_tuple_to_str(pool_fingerprint(p))
        if eng_fp != r["row_fingerprint"]:
            fp_mismatch.append(r["pool_id"])
    return {
        "pass": not missing and not extra and not fp_mismatch,
        "missing": missing,
        "extra": extra,
        "fingerprint_mismatch": fp_mismatch,
    }

"""Pilot-line catalog overlap vs production publish (global snapshot_id)."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import clickhouse_connect

from ...clickhouse_config import load_clickhouse_config
from ..active_snapshot.pool_catalog_schema import TABLE_ACTIVE_POOL
from .catalog_integrity import CatalogIdentityError, compare_catalog_rows_to_json_fields
from .historical_batch_config import (
    PILOT_COMBO_FOLDER,
    PILOT_DECISION_CLOSE_STEMS,
    PILOT_MONTH,
    PILOT_PIPELINE_LINE_ID,
)
from .parse import build_import_bundle, load_manifest_context, snapshot_path_for_stem
from ..identity import build_live_micro_lld_v2_config, config_hash


def pilot_snapshot_ids_from_catalog(catalog_root) -> set[str]:
    """Derive pilot snapshot_ids from JSON (no ClickHouse)."""
    from pathlib import Path

    root = Path(catalog_root)
    manifest = load_manifest_context(root)
    ch = config_hash(build_live_micro_lld_v2_config(10))
    ids: set[str] = set()
    for stem in PILOT_DECISION_CLOSE_STEMS:
        path = snapshot_path_for_stem(
            root, month=PILOT_MONTH, combo=PILOT_COMBO_FOLDER, stem=stem
        )
        bundle = build_import_bundle(
            path,
            catalog_root=root,
            config_hash=ch,
            manifest=manifest,
            pipeline_line_id=PILOT_PIPELINE_LINE_ID,
        )
        ids.add(str(bundle["snapshot_id"]))
    return ids


def load_ch_catalog_rows(snapshot_id: str) -> list[dict[str, Any]]:
    ch_cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**ch_cfg.connect_kwargs())
    db = ch_cfg.database
    try:
        q = f"""
        SELECT pool_id, side, pool_type, top_price, bottom_price, strength, strength_class,
               known_at, row_fingerprint, pipeline_line_id
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
                "row_fingerprint": r[8],
                "pipeline_line_id": r[9],
            }
            for r in client.query(q, parameters={"sid": snapshot_id}).result_rows
        ]
    finally:
        client.close()


def verify_ch_catalog_matches_import_bundle(
    catalog_rows: Sequence[Mapping[str, Any]],
    *,
    snapshot_id: str,
    json_pools_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    """
    Fail closed when global catalog exists but import bundle disagrees (overlap safety).
    """
    ch_rows = load_ch_catalog_rows(snapshot_id)
    if not ch_rows:
        return
    ch_by_id = {r["pool_id"]: r for r in ch_rows}
    import_ids = {str(r["pool_id"]) for r in catalog_rows}
    if set(ch_by_id) != import_ids:
        raise CatalogIdentityError(
            f"CH/import pool_id set mismatch for {snapshot_id}: "
            f"ch={len(ch_by_id)} import={len(import_ids)}"
        )
    if json_pools_by_id:
        jm = compare_catalog_rows_to_json_fields(catalog_rows, json_pools_by_id)
        if jm:
            raise CatalogIdentityError(f"import vs JSON pools: {jm[0]}")
    for row in catalog_rows:
        pid = str(row["pool_id"])
        ch = ch_by_id.get(pid)
        if ch is None:
            raise CatalogIdentityError(f"CH missing catalog pool {pid} for {snapshot_id}")
        if str(ch["row_fingerprint"]) != str(row["row_fingerprint"]):
            raise CatalogIdentityError(
                f"row_fingerprint mismatch CH vs import for {pid} on {snapshot_id}"
            )
        for field in ("side", "top_price", "bottom_price", "strength", "strength_class"):
            jv, cv = row.get(field), ch.get(field)
            if field in ("top_price", "bottom_price", "strength"):
                if float(jv) != float(cv):
                    raise CatalogIdentityError(f"{pid}.{field} CH vs import")
            elif str(jv) != str(cv):
                raise CatalogIdentityError(f"{pid}.{field} CH vs import")


def describe_overlap_behavior() -> dict[str, str]:
    return {
        "global_snapshot_id": "meta/member keyed by snapshot_id only; one physical ACTIVE state.",
        "catalog_storage": "MergeTree ORDER BY (snapshot_id, pool_id); skip insert when count>0.",
        "catalog_pipeline_line_id": "Pilot rows retain pilot pipeline_line_id; readers load by snapshot_id.",
        "production_publish": "New publish row on production line; no second catalog insert.",
        "reader": "PublishedActiveReader uses publish line + snapshot_id; catalog not filtered by line.",
        "conflict": "verify_ch_catalog_matches_import_bundle before publish on overlap path.",
    }

"""Fail-closed validation for historical JSON → CH import bundles."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from research_charts.historical_lmv2.decision_snapshot import validate_snapshot_active_state
from research_charts.historical_lmv2.mixed_catalog import discover_mixed_frozen_catalog
from research_charts.historical_lmv2.provenance_lookup import relative_dest_for
from research_charts.trp_import import ensure_trp_on_path

from ..serialize import row_fingerprint
from .fingerprint_parse import enrich_pool_row_from_json


def _fp_tuple_to_str(fp: tuple) -> str:
    return "|".join(str(x) for x in fp)


def validate_provenance(catalog_root: Path, bundle: dict[str, Any]) -> None:
    catalog = discover_mixed_frozen_catalog(catalog_root)
    rel = relative_dest_for(catalog_root, bundle["path"])
    payload = bundle["payload"]
    frozen_id = str(bundle.get("frozen_input_id") or "")
    if frozen_id not in catalog.authorized_frozen_input_ids:
        raise ValueError(f"frozen_input_id {frozen_id!r} not in catalog manifest")
    catalog.provenance.verify_file(rel, bundle["path"], frozen_id)


def validate_snapshot_semantics(bundle: dict[str, Any]) -> None:
    payload = bundle["payload"]
    state = bundle["state"]
    validate_snapshot_active_state(
        payload,
        symbol=state.symbol,
        timeframe=state.timeframe,
        pool_detail=state.pool_detail,
        decision_close=state.decision_close,
    )
    if len(bundle["catalog_rows"]) != state.active_pool_count:
        raise ValueError(
            f"catalog row count {len(bundle['catalog_rows'])} != active_pool_count {state.active_pool_count}"
        )
    ids = {r["pool_id"] for r in bundle["catalog_rows"]}
    if ids != set(state.active_pool_ids):
        raise ValueError("catalog pool_id set != active_pool_ids")


def validate_row_fingerprints(bundle: dict[str, Any]) -> None:
    ensure_trp_on_path()
    payload_pools = (bundle.get("payload") or {}).get("pools") or {}
    for row in bundle["catalog_rows"]:
        src = payload_pools.get(row["pool_id"]) or {}
        src_fp = str(src.get("row_fingerprint") or "")
        enriched = enrich_pool_row_from_json(dict(src), active=True)
        computed = _fp_tuple_to_str(row_fingerprint(enriched))
        if computed != row["row_fingerprint"]:
            raise ValueError(f"row_fingerprint mismatch for {row['pool_id']}")
        if row["row_fingerprint"] != src_fp:
            raise ValueError(f"row_fingerprint drift for {row['pool_id']}")


def validate_bundle(catalog_root: Path, bundle: dict[str, Any]) -> None:
    validate_provenance(catalog_root, bundle)
    validate_snapshot_semantics(bundle)
    validate_row_fingerprints(bundle)

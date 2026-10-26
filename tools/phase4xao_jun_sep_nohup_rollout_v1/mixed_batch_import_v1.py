"""
Phase 4X-AT — mixed-batch import: only missing stems through execute_batch_import.

Existing publishes are verified against their **stored** sidecar identity (including
their original provenance_lookup_version), not the current catalog lookup digest.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from research_charts.live_micro_lld_v2_ch_test.historical_json_import.catalog_inventory import (
    stem_path,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.parse import (
    build_import_bundle,
    load_manifest_context,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.provenance_identity import (
    assert_canonical_original_match,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.provenance_sidecar import (
    ProvenanceSidecarConflictError,
    build_sidecar_row_from_bundle,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.provenance_sidecar_store import (
    ClickHouseProvenanceSidecarStore,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.validate import validate_bundle
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.catalog_integrity import (
    verify_ch_catalog_identity,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.catalog_write import (
    catalog_row_count,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_overlap import (
    verify_ch_catalog_matches_import_bundle,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.pool_catalog_schema import (
    TABLE_ACTIVE_POOL,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.publisher import resolve_existing_publish
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.extract import ActiveStateSnapshot
from research_charts.clickhouse_config import load_clickhouse_config
import clickhouse_connect


class MixedBatchIntegrityError(RuntimeError):
    """Published stem cannot be skipped safely."""


def stems_to_import(batch_stems: Sequence[str], published_stems: set[str]) -> list[str]:
    return [s for s in batch_stems if s not in published_stems]


def assert_published_stems_integrity(
    *,
    catalog_root: Path,
    month: str,
    combo_folder: str,
    pipeline_line_id: str,
    config_hash: str,
    published_stems: set[str],
    batch_stems: Sequence[str],
) -> None:
    """
    For stems already in ``published_stems``, verify publish + catalog + sidecar
    match frozen JSON (canonical original fields at **stored** lookup version).
    """
    skip = [s for s in batch_stems if s in published_stems]
    if not skip:
        return
    manifest = load_manifest_context(catalog_root)
    store = ClickHouseProvenanceSidecarStore()
    ch_cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**ch_cfg.connect_kwargs())
    db = ch_cfg.database
    try:
        for stem in skip:
            path = stem_path(catalog_root, month, combo_folder, stem)
            bundle = build_import_bundle(
                path,
                catalog_root=catalog_root,
                config_hash=config_hash,
                manifest=manifest,
                pipeline_line_id=pipeline_line_id,
            )
            validate_bundle(catalog_root, bundle)
            state: ActiveStateSnapshot = bundle["state"]
            existing = resolve_existing_publish(
                client,
                db,
                state,
                pipeline_line_id=pipeline_line_id,
                snapshot_id=bundle["snapshot_id"],
                allow_existing_publish=True,
            )
            if not existing:
                raise MixedBatchIntegrityError(
                    f"{stem}: in published_stems set but no FINAL publish for {bundle['snapshot_id']}"
                )
            stored = store.read(bundle["snapshot_id"], pipeline_line_id)
            if stored is None:
                raise MixedBatchIntegrityError(
                    f"{stem}: publish exists but sidecar missing for {bundle['snapshot_id']}"
                )
            expected = build_sidecar_row_from_bundle(
                bundle,
                pipeline_line_id=pipeline_line_id,
                import_batch_id=stored.import_batch_id,
                catalog_manifest_id=stored.catalog_manifest_id,
                provenance_lookup_version=stored.provenance_lookup_version,
                catalog_root=catalog_root,
            )
            assert_canonical_original_match(
                stored,
                expected,
                context=f"skip-import integrity {stem}",
            )
            expected_n = state.active_pool_count
            cat_n = catalog_row_count(bundle["snapshot_id"]) if expected_n else 0
            if cat_n != expected_n:
                raise MixedBatchIntegrityError(
                    f"{stem}: catalog {cat_n}/{expected_n} for {bundle['snapshot_id']}"
                )
            verify_ch_catalog_identity(
                client, db, TABLE_ACTIVE_POOL, bundle["snapshot_id"], state.active_pool_ids
            )
            verify_ch_catalog_matches_import_bundle(
                bundle["catalog_rows"],
                snapshot_id=bundle["snapshot_id"],
            )
    finally:
        client.close()


def provenance_lookup_epochs_compatible() -> dict[str, str]:
    """
    Architecture note: provenance_lookup_version is per sidecar row (per snapshot_id),
    not a single epoch for the whole month. Multiple lookup digests on one pipeline line
    are expected when catalog provenance_lookup.json is updated between import waves.
    """
    return {
        "model": "per_snapshot_sidecar",
        "guard": "assert_canonical_original_match on re-import only",
        "mixed_batch_policy": "skip import path; stored lookup preserved",
    }

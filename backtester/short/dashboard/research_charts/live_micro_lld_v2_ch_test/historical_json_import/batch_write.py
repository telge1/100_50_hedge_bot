"""Gated batch write: provenance-before-publish via ``import_historical_close``."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from research_charts.trp_import import ensure_trp_on_path

from ..active_snapshot.publish_guard import LineIdentity
from .batch_runner import BatchImportError, BatchSpec, validate_batch
from .import_close import import_historical_close
from .catalog_inventory import stem_path
from .parse import build_import_bundle, load_manifest_context
from .validate import validate_bundle
from ..identity import build_live_micro_lld_v2_config, config_hash
from .historical_batch_config import combo_for_folder
from .provenance_sidecar import build_sidecar_row_from_bundle, provenance_lookup_version
from .historical_import_lock import historical_import_write_lock
from .write_guards import require_valid_test_run_uuid
from .productive_prewrite_gate import (
    ProductivePrewriteGateError,
    assert_productive_env_gates,
    run_productive_prewrite_gate,
)
from .provenance_sidecar_store import (
    ProvenanceSidecarStore,
    default_sidecar_store_for_batch,
)

_WRITE_ENV = "HISTORICAL_JSON_IMPORT_ALLOW_WRITE"


def execute_batch_import(
    catalog_root: Path,
    spec: BatchSpec,
    *,
    test_run_id: str | None = None,
    sidecar_store: ProvenanceSidecarStore | None = None,
    write_lock_held: bool = False,
) -> list[dict[str, Any]]:
    assert_productive_env_gates()
    run_id = require_valid_test_run_uuid(
        test_run_id if test_run_id is not None else str(uuid.uuid4()),
        context="execute_batch_import",
    )
    vr = validate_batch(catalog_root, spec, test_run_id=run_id)
    if vr["errors"]:
        raise BatchImportError(f"batch validate failed: {vr['errors'][:5]}")
    from .productive_prewrite_gate import (
        assess_scope_ch_states,
        load_scope_layer_map_for_snapshots,
    )
    from .ch_import_state import ch_client_and_db, fetch_published_snapshot_ids

    layer_map = load_scope_layer_map_for_snapshots(vr.get("snapshot_ids") or [])
    client, db = ch_client_and_db()
    try:
        published = fetch_published_snapshot_ids(
            client, db, pipeline_line_id=spec.pipeline_line_id
        )
    finally:
        client.close()
    scope_checks = assess_scope_ch_states(
        vr.get("snapshot_ids") or [],
        pipeline_line_id=spec.pipeline_line_id,
        published_on_line=published,
        layer_by_snapshot=layer_map,
    )
    run_id = run_productive_prewrite_gate(
        test_run_id=run_id,
        pipeline_line_id=spec.pipeline_line_id,
        spec=spec,
        scope_ch_checks=scope_checks,
    ).test_run_id
    ensure_trp_on_path()
    combo = combo_for_folder(spec.combo_folder)
    if combo is None:
        raise BatchImportError(f"unknown combo {spec.combo_folder}")
    ch = config_hash(build_live_micro_lld_v2_config(combo.pool_detail))
    manifest = load_manifest_context(catalog_root)
    store = sidecar_store if sidecar_store is not None else default_sidecar_store_for_batch()
    lookup_ver = provenance_lookup_version(catalog_root)
    if write_lock_held:
        return _execute_batch_import_locked(
            catalog_root,
            spec,
            ch=ch,
            manifest=manifest,
            run_id=run_id,
            store=store,
            lookup_ver=lookup_ver,
        )
    with historical_import_write_lock():
        return _execute_batch_import_locked(
            catalog_root,
            spec,
            ch=ch,
            manifest=manifest,
            run_id=run_id,
            store=store,
            lookup_ver=lookup_ver,
        )


def _execute_batch_import_locked(
    catalog_root: Path,
    spec: BatchSpec,
    *,
    ch: str,
    manifest: Any,
    run_id: str,
    store: ProvenanceSidecarStore,
    lookup_ver: str,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for stem in spec.stems:
        path = stem_path(catalog_root, spec.month, spec.combo_folder, stem)
        bundle = build_import_bundle(
            path,
            catalog_root=catalog_root,
            config_hash=ch,
            manifest=manifest,
            pipeline_line_id=spec.pipeline_line_id,
        )
        validate_bundle(catalog_root, bundle)
        sidecar_row = build_sidecar_row_from_bundle(
            bundle,
            pipeline_line_id=spec.pipeline_line_id,
            import_batch_id=spec.import_batch_id,
            catalog_manifest_id=manifest.catalog_id,
            provenance_lookup_version=lookup_ver,
            catalog_root=catalog_root,
        )
        state = bundle["state"]
        line = LineIdentity(
            symbol=state.symbol,
            timeframe=state.timeframe,
            pool_detail=state.pool_detail,
            config_hash=state.config_hash,
            engine_version=state.engine_version,
            candle_load_start=state.candle_load_start,
            history_from_close=state.history_from_close,
            history_policy=state.history_policy,
        )
        outcome = import_historical_close(
            state,
            bundle["catalog_rows"],
            pipeline_line_id=spec.pipeline_line_id,
            test_run_id=run_id,
            line=line,
            sidecar_store=store,
            sidecar_row=sidecar_row,
        )
        results.append(
            {
                "stem": stem,
                "snapshot_id": bundle["snapshot_id"],
                "import_batch_id": spec.import_batch_id,
                **outcome,
            }
        )
    return results

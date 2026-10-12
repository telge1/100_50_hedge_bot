"""
Post-import readback verification for a controlled run manifest (read-only CH).

Uses ``run_batch_readback_gate`` with the supported public signature (no ``fail_closed``
kwarg — fail-closed parity is always enabled for ClickHouse readback inside that helper).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .batch_runner import run_batch_readback_gate, validate_batch
from .ch_scope_query import scope_layer_counts_batched
from .controlled_run_manifest import (
    ControlledRunManifest,
    iter_manifest_batch_specs,
    load_controlled_run_manifest,
    resolve_manifest_stems,
)


def verify_manifest_scope_readback(
    catalog_root: Path,
    manifest: ControlledRunManifest,
    *,
    test_run_id: str | None = None,
) -> dict[str, Any]:
    stems = resolve_manifest_stems(catalog_root, manifest)
    specs = iter_manifest_batch_specs(catalog_root, manifest, stems)
    parity_failures = 0
    sidecar_failures = 0
    batch_results: list[dict[str, Any]] = []
    for spec in specs:
        if test_run_id:
            vr = validate_batch(catalog_root, spec, test_run_id=test_run_id)
            if vr.get("errors"):
                batch_results.append(
                    {
                        "import_batch_id": spec.import_batch_id,
                        "status": "validate_failed",
                        "errors": vr["errors"],
                    }
                )
                continue
        rb = run_batch_readback_gate(
            list(spec.stems),
            catalog_root=catalog_root,
            combo_folder=spec.combo_folder,
            month=spec.month,
            pipeline_line_id=spec.pipeline_line_id,
            batch_spec=spec,
            require_sidecar=True,
        )
        bad_p = sum(1 for r in rb["parity"] if not r.ok)
        bad_s = sum(1 for r in rb["sidecar"] if not r.ok)
        parity_failures += bad_p
        sidecar_failures += bad_s
        batch_results.append(
            {
                "import_batch_id": spec.import_batch_id,
                "status": "ok" if not bad_p and not bad_s else "readback_failed",
                "parity_failures": bad_p,
                "sidecar_failures": bad_s,
            }
        )
    return {
        "stems": len(stems),
        "batches": len(specs),
        "parity_failures": parity_failures,
        "sidecar_failures": sidecar_failures,
        "ok": parity_failures == 0 and sidecar_failures == 0,
        "batch_results": batch_results,
    }


def verify_manifest_scope_ch_counts(
    catalog_root: Path,
    manifest: ControlledRunManifest,
    *,
    snapshot_ids: list[str] | None = None,
    chunk_size: int = 100,
) -> dict[str, int]:
    """
    Post-import CH row counts for manifest scope using batched ``snapshot_id`` queries.
    """
    from .ch_import_state import ch_client_and_db

    if snapshot_ids is None:
        specs = iter_manifest_batch_specs(
            catalog_root, manifest, resolve_manifest_stems(catalog_root, manifest)
        )
        snapshot_ids = []
        for spec in specs:
            vr = validate_batch(catalog_root, spec)
            snapshot_ids.extend(vr.get("snapshot_ids") or [])
    client, db = ch_client_and_db()
    try:
        return scope_layer_counts_batched(
            client,
            db,
            snapshot_ids,
            pipeline_line_id=manifest.pipeline_line_id,
            chunk_size=chunk_size,
        )
    finally:
        client.close()


def verify_manifest_file(
    catalog_root: Path,
    manifest_path: Path,
    *,
    test_run_id: str | None = None,
) -> dict[str, Any]:
    manifest = load_controlled_run_manifest(manifest_path)
    out = verify_manifest_scope_readback(
        catalog_root, manifest, test_run_id=test_run_id
    )
    out["manifest_path"] = str(manifest_path)
    out["scope_fingerprint"] = manifest.scope_fingerprint
    return out

"""Pilot-line provenance validation (read-only JSON + lookup; no CH mutation)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .historical_batch_config import (
    PILOT_COMBO_FOLDER,
    PILOT_DECISION_CLOSE_STEMS,
    PILOT_MONTH,
    PILOT_PIPELINE_LINE_ID,
)
from .parse import build_import_bundle, load_manifest_context, snapshot_path_for_stem
from .provenance_sidecar import build_sidecar_row_from_bundle, provenance_lookup_version
from .validate import validate_bundle
from ..identity import build_live_micro_lld_v2_config, config_hash


def validate_pilot_provenance(catalog_root: Path) -> dict[str, Any]:
    """
    Re-validate 10 pilot snapshots: SHA256 + frozen id via ``validate_provenance``.

    Does not insert sidecar or alter pilot publish rows.
    """
    manifest = load_manifest_context(catalog_root)
    ch = config_hash(build_live_micro_lld_v2_config(10))
    lookup_ver = provenance_lookup_version(catalog_root)
    errors: list[str] = []
    rows: list[dict[str, str]] = []
    for stem in PILOT_DECISION_CLOSE_STEMS:
        path = snapshot_path_for_stem(
            catalog_root, month=PILOT_MONTH, combo=PILOT_COMBO_FOLDER, stem=stem
        )
        try:
            bundle = build_import_bundle(
                path,
                catalog_root=catalog_root,
                config_hash=ch,
                manifest=manifest,
                pipeline_line_id=PILOT_PIPELINE_LINE_ID,
            )
            validate_bundle(catalog_root, bundle)
            sidecar = build_sidecar_row_from_bundle(
                bundle,
                pipeline_line_id=PILOT_PIPELINE_LINE_ID,
                import_batch_id="pilot_phase3d_retroactive",
                catalog_manifest_id=manifest.catalog_id,
                provenance_lookup_version=lookup_ver,
                catalog_root=catalog_root,
            )
            rows.append(sidecar.as_ch_dict())
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{stem}: {exc}")
    return {
        "pilot_close_count": len(PILOT_DECISION_CLOSE_STEMS),
        "errors": errors,
        "planned_sidecar_rows": len(rows),
        "ok": not errors,
        "note": "Retroactive sidecar INSERT is separate approval; pilot publish unchanged.",
    }

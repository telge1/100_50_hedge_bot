"""Sidecar parity: JSON + lookup vs ClickHouse (read-only)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..identity import build_live_micro_lld_v2_config, config_hash
from .batch_runner import BatchSpec
from .catalog_inventory import stem_path
from .historical_batch_config import combo_for_folder
from .parse import build_import_bundle, load_manifest_context
from .provenance_sidecar import build_sidecar_row_from_bundle, provenance_lookup_version
from .provenance_sidecar_store import ProvenanceSidecarStore, evaluate_physical_duplicates
from .readback_errors import ReadbackGateFailure
from .validate import validate_bundle
from research_charts.trp_import import ensure_trp_on_path


@dataclass
class SidecarReadbackReport:
    stem: str
    snapshot_id: str
    ok: bool
    mismatches: list[str] = field(default_factory=list)


def expected_sidecar_for_stem(
    catalog_root: Path,
    spec: BatchSpec,
    stem: str,
    *,
    lookup_ver: str | None = None,
    manifest: Any | None = None,
    config_hash_value: str | None = None,
) -> Any:
    ensure_trp_on_path()
    combo = combo_for_folder(spec.combo_folder)
    if combo is None:
        raise ValueError(f"unknown combo {spec.combo_folder}")
    ch = config_hash_value or config_hash(build_live_micro_lld_v2_config(combo.pool_detail))
    manifest = manifest or load_manifest_context(catalog_root)
    lookup_ver = lookup_ver or provenance_lookup_version(catalog_root)
    path = stem_path(catalog_root, spec.month, spec.combo_folder, stem)
    bundle = build_import_bundle(
        path,
        catalog_root=catalog_root,
        config_hash=ch,
        manifest=manifest,
        pipeline_line_id=spec.pipeline_line_id,
    )
    validate_bundle(catalog_root, bundle)
    return build_sidecar_row_from_bundle(
        bundle,
        pipeline_line_id=spec.pipeline_line_id,
        import_batch_id=spec.import_batch_id,
        catalog_manifest_id=manifest.catalog_id,
        provenance_lookup_version=lookup_ver,
        catalog_root=catalog_root,
    )


def verify_sidecar_readback_for_batch(
    catalog_root: Path,
    spec: BatchSpec,
    store: ProvenanceSidecarStore,
    *,
    stems: Sequence[str] | None = None,
    fail_closed: bool = True,
) -> list[SidecarReadbackReport]:
    """
    Each expected provenance row must exist in CH with matching original fields.
    Detects missing rows, canonical mismatches, and conflicting physical duplicates.
    """
    stems = list(stems or spec.stems)
    lookup_ver = provenance_lookup_version(catalog_root)
    manifest = load_manifest_context(catalog_root)
    combo = combo_for_folder(spec.combo_folder)
    ch = config_hash(build_live_micro_lld_v2_config(combo.pool_detail)) if combo else ""
    reports: list[SidecarReadbackReport] = []
    for stem in stems:
        expected = expected_sidecar_for_stem(
            catalog_root,
            spec,
            stem,
            lookup_ver=lookup_ver,
            manifest=manifest,
            config_hash_value=ch,
        )
        mismatches: list[str] = []
        physical = store.list_physical_rows(
            expected.snapshot_id,
            expected.pipeline_line_id,
        )
        dup_err = evaluate_physical_duplicates(physical)
        if dup_err:
            mismatches.append(dup_err)
        stored = store.read(expected.snapshot_id, expected.pipeline_line_id)
        if stored is None:
            mismatches.append(f"missing sidecar for {expected.snapshot_id}")
        else:
            for field in (
                "dest_sha256",
                "frozen_input_id",
                "relative_json_path",
                "catalog_manifest_id",
                "provenance_lookup_version",
                "snapshot_id",
            ):
                if getattr(stored, field) != getattr(expected, field):
                    mismatches.append(
                        f"{field}: expected={getattr(expected, field)!r} "
                        f"actual={getattr(stored, field)!r}"
                    )
        reports.append(
            SidecarReadbackReport(
                stem=stem,
                snapshot_id=expected.snapshot_id,
                ok=not mismatches,
                mismatches=mismatches,
            )
        )
    if fail_closed:
        bad = [r for r in reports if not r.ok]
        if bad:
            raise ReadbackGateFailure(bad)
    return reports

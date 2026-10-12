"""Mixed-frozen historical V2 catalog (per-file provenance, fail-closed)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .catalog import ProductionCatalog, _load_json
from .errors import ManifestGateError
from .provenance_lookup import ProvenanceLookup, load_provenance_lookup


@dataclass(frozen=True)
class MixedFrozenProductionCatalog(ProductionCatalog):
    """Catalog with multiple authorized snapshot frozen_input lineages."""

    catalog_manifest: dict
    canonical_candle_input_id: str
    authorized_frozen_input_ids: frozenset[str]
    allowed_coverage: frozenset[tuple[str, str]]
    provenance: ProvenanceLookup

    @property
    def mixed_frozen(self) -> bool:
        return True


def _coverage_allowed(coverage_path: Path) -> frozenset[tuple[str, str]]:
    if not coverage_path.is_file():
        raise ManifestGateError(f"missing coverage matrix {coverage_path}")
    rows = _load_json(coverage_path).get("MONTH_TF_PD_COVERAGE") or []
    allowed: set[tuple[str, str]] = set()
    for row in rows:
        if str(row.get("STATUS")) != "PASS":
            continue
        if int(row.get("MISSING") or 0) != 0:
            raise ManifestGateError(f"coverage not complete for {row.get('month')} {row.get('combo')}")
        allowed.add((str(row["month"]), str(row["combo"])))
    if not allowed:
        raise ManifestGateError("coverage matrix has no PASS rows")
    return frozenset(allowed)


def discover_mixed_frozen_catalog(production_root: Path, *, name: str | None = None) -> MixedFrozenProductionCatalog:
    root = production_root.resolve()
    manifest_path = root / "production" / "catalog_manifest.json"
    if not manifest_path.is_file():
        raise ManifestGateError(f"missing catalog_manifest.json under {root}")
    manifest = _load_json(manifest_path)
    catalog_id = str(manifest["catalog_id"])
    if name and catalog_id != name:
        raise ManifestGateError("catalog_id mismatch between registry and manifest")
    canonical = str(manifest["canonical_candle_input_id"])
    authorized = frozenset(str(x) for x in manifest["authorized_frozen_input_ids"])
    if canonical not in authorized:
        raise ManifestGateError("canonical_candle_input_id must be in authorized_frozen_input_ids")

    coverage_ref = manifest.get("coverage_matrix_path") or "production/coverage_matrix.json"
    coverage_path = root / coverage_ref
    allowed = _coverage_allowed(coverage_path)

    lookup_ref = manifest.get("provenance_lookup_path") or "production/provenance_lookup.json"
    provenance = load_provenance_lookup(root, lookup_ref)

    if str(manifest.get("data_release_status") or "") != "RELEASED":
        raise ManifestGateError("catalog data_release_status is not RELEASED")

    return MixedFrozenProductionCatalog(
        name=name or catalog_id,
        production_root=root,
        frozen_input_id=canonical,
        releases=(),
        catalog_manifest=manifest,
        canonical_candle_input_id=canonical,
        authorized_frozen_input_ids=authorized,
        allowed_coverage=allowed,
        provenance=provenance,
    )


def assert_mixed_coverage(catalog: MixedFrozenProductionCatalog, month: str, combo: str) -> None:
    if (month, combo) not in catalog.allowed_coverage:
        raise ManifestGateError(f"{month} {combo} not in released coverage for {catalog.name}")

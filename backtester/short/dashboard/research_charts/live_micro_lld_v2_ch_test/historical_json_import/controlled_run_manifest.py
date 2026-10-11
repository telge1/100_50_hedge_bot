"""
Immutable approved-scope manifest for controlled monthly historical imports.

The orchestrator must not expand beyond the frozen manifest bounds on resume.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from .batch_runner import BatchImportError, BatchSpec
from .catalog_inventory import iter_stems
from .historical_batch_config import (
    MAX_BATCH_SIZE_CLOSES,
    combo_for_folder,
)


MANIFEST_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class StemSlice:
    start_index: int
    end_index: int

    def resolve(self, all_stems: tuple[str, ...]) -> tuple[str, ...]:
        if self.start_index < 0 or self.end_index < self.start_index:
            raise ValueError("invalid stem slice indices")
        chunk = all_stems[self.start_index : self.end_index]
        if len(chunk) != self.end_index - self.start_index:
            raise BatchImportError(
                f"stem slice [{self.start_index}:{self.end_index}] resolved "
                f"{len(chunk)} stems, catalog has {len(all_stems)}"
            )
        return tuple(chunk)


@dataclass(frozen=True)
class ControlledRunManifest:
    schema_version: int
    run_id: str
    approved_month: str
    combo_folder: str
    pipeline_line_id: str
    stem_slice: StemSlice
    max_snapshots: int
    max_batches: int
    batch_size: int
    import_batch_id: str
    backup_recovery_id: str
    scope_fingerprint: str
    batch_import_ids: tuple[str, ...] = ()

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "approved_month": self.approved_month,
            "combo_folder": self.combo_folder,
            "pipeline_line_id": self.pipeline_line_id,
            "stem_slice": {
                "start_index": self.stem_slice.start_index,
                "end_index": self.stem_slice.end_index,
            },
            "max_snapshots": self.max_snapshots,
            "max_batches": self.max_batches,
            "batch_size": self.batch_size,
            "import_batch_id": self.import_batch_id,
            "backup_recovery_id": self.backup_recovery_id,
            "scope_fingerprint": self.scope_fingerprint,
            "batch_import_ids": list(self.batch_import_ids),
        }


def compute_scope_fingerprint(
    *,
    approved_month: str,
    combo_folder: str,
    pipeline_line_id: str,
    stem_slice: StemSlice,
    stems: tuple[str, ...],
) -> str:
    payload = {
        "month": approved_month,
        "combo_folder": combo_folder,
        "pipeline_line_id": pipeline_line_id,
        "start_index": stem_slice.start_index,
        "end_index": stem_slice.end_index,
        "stems": list(stems),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def load_controlled_run_manifest(path: Path) -> ControlledRunManifest:
    data = json.loads(path.read_text(encoding="utf-8"))
    return manifest_from_dict(data)


def manifest_from_dict(data: dict[str, Any]) -> ControlledRunManifest:
    if int(data.get("schema_version", 0)) != MANIFEST_SCHEMA_VERSION:
        raise BatchImportError(f"unsupported manifest schema: {data.get('schema_version')}")
    run_id = str(data["run_id"])
    UUID(run_id)
    slice_raw = data["stem_slice"]
    stem_slice = StemSlice(
        start_index=int(slice_raw["start_index"]),
        end_index=int(slice_raw["end_index"]),
    )
    batch_size = int(data["batch_size"])
    if batch_size < 1 or batch_size > MAX_BATCH_SIZE_CLOSES:
        raise BatchImportError(f"batch_size out of range: {batch_size}")
    return ControlledRunManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        run_id=run_id,
        approved_month=str(data["approved_month"]),
        combo_folder=str(data["combo_folder"]),
        pipeline_line_id=str(data["pipeline_line_id"]),
        stem_slice=stem_slice,
        max_snapshots=int(data["max_snapshots"]),
        max_batches=int(data["max_batches"]),
        batch_size=batch_size,
        import_batch_id=str(data["import_batch_id"]),
        backup_recovery_id=str(data["backup_recovery_id"]),
        scope_fingerprint=str(data["scope_fingerprint"]),
        batch_import_ids=tuple(str(x) for x in (data.get("batch_import_ids") or ())),
    )


def resolve_manifest_stems(
    catalog_root: Path,
    manifest: ControlledRunManifest,
) -> tuple[str, ...]:
    combo = combo_for_folder(manifest.combo_folder)
    if combo is None:
        raise BatchImportError(f"unknown combo {manifest.combo_folder}")
    if combo.pipeline_line_id() != manifest.pipeline_line_id:
        raise BatchImportError(
            f"pipeline_line_id mismatch manifest={manifest.pipeline_line_id} "
            f"combo={combo.pipeline_line_id()}"
        )
    all_stems = tuple(
        iter_stems(
            catalog_root,
            month=manifest.approved_month,
            combo_folder=manifest.combo_folder,
        )
    )
    stems = manifest.stem_slice.resolve(all_stems)
    if len(stems) > manifest.max_snapshots:
        raise BatchImportError(
            f"resolved {len(stems)} stems exceeds max_snapshots={manifest.max_snapshots}"
        )
    fp = compute_scope_fingerprint(
        approved_month=manifest.approved_month,
        combo_folder=manifest.combo_folder,
        pipeline_line_id=manifest.pipeline_line_id,
        stem_slice=manifest.stem_slice,
        stems=stems,
    )
    if fp != manifest.scope_fingerprint:
        raise BatchImportError(
            "scope_fingerprint mismatch — manifest bounds do not match frozen catalog stems"
        )
    return stems


def iter_manifest_batch_specs(
    catalog_root: Path,
    manifest: ControlledRunManifest,
    stems: tuple[str, ...],
) -> list[BatchSpec]:
    if not stems:
        raise BatchImportError("empty stem list for manifest")
    specs: list[BatchSpec] = []
    batch_idx = 0
    for i in range(0, len(stems), manifest.batch_size):
        chunk = stems[i : i + manifest.batch_size]
        if manifest.batch_import_ids:
            if batch_idx >= len(manifest.batch_import_ids):
                raise BatchImportError("batch_import_ids shorter than planned batches")
            bid = manifest.batch_import_ids[batch_idx]
        elif len(stems) <= manifest.batch_size:
            bid = manifest.import_batch_id
        else:
            bid = f"{manifest.import_batch_id}_batch{batch_idx + 1:02d}"
        specs.append(
            BatchSpec(
                month=manifest.approved_month,
                combo_folder=manifest.combo_folder,
                stems=chunk,
                pipeline_line_id=manifest.pipeline_line_id,
                import_batch_id=bid,
            )
        )
        batch_idx += 1
    if len(specs) > manifest.max_batches:
        raise BatchImportError(
            f"plan needs {len(specs)} batches > max_batches={manifest.max_batches}"
        )
    return specs


def build_phase4i_april_final_30_manifest(catalog_root: Path) -> ControlledRunManifest:
    """April 2026 4h_PD10 stems[150:180] — planning helper (writes manifest JSON)."""
    from .historical_batch_config import StrategyCombo

    combo = StrategyCombo("4h", 10)
    month = "2026-04"
    stem_slice = StemSlice(150, 180)
    all_stems = tuple(iter_stems(catalog_root, month=month, combo_folder=combo.folder))
    stems = stem_slice.resolve(all_stems)
    run_id = "00000000-0000-4000-8000-000000004101"
    fp = compute_scope_fingerprint(
        approved_month=month,
        combo_folder=combo.folder,
        pipeline_line_id=combo.pipeline_line_id(),
        stem_slice=stem_slice,
        stems=stems,
    )
    return ControlledRunManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        run_id=run_id,
        approved_month=month,
        combo_folder=combo.folder,
        pipeline_line_id=combo.pipeline_line_id(),
        stem_slice=stem_slice,
        max_snapshots=30,
        max_batches=1,
        batch_size=50,
        import_batch_id="phase4i_final_30_apr2026_4hpd10_v1",
        backup_recovery_id="e134b0fa-9cba-4c0f-b8d9-60125001de50",
        scope_fingerprint=fp,
    )


def build_phase4k_april_15mpd1_500_manifest(catalog_root: Path) -> ControlledRunManifest:
    """April 2026 15m_PD1 stems[0:500] — multi-batch dry-run / approval manifest."""
    from .historical_batch_config import StrategyCombo

    combo = StrategyCombo("15m", 1)
    month = "2026-04"
    stem_slice = StemSlice(0, 500)
    all_stems = tuple(iter_stems(catalog_root, month=month, combo_folder=combo.folder))
    stems = stem_slice.resolve(all_stems)
    run_id = "00000000-0000-4000-8000-000000004b01"
    fp = compute_scope_fingerprint(
        approved_month=month,
        combo_folder=combo.folder,
        pipeline_line_id=combo.pipeline_line_id(),
        stem_slice=stem_slice,
        stems=stems,
    )
    prefix = "phase4k_apr2026_15mpd1_v1"
    batch_ids = tuple(f"{prefix}_batch{b:02d}" for b in range(1, 11))
    return ControlledRunManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        run_id=run_id,
        approved_month=month,
        combo_folder=combo.folder,
        pipeline_line_id=combo.pipeline_line_id(),
        stem_slice=stem_slice,
        max_snapshots=500,
        max_batches=10,
        batch_size=50,
        import_batch_id=prefix,
        backup_recovery_id="e134b0fa-9cba-4c0f-b8d9-60125001de50",
        scope_fingerprint=fp,
        batch_import_ids=batch_ids,
    )

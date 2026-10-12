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


def build_phase4o_april_15mpd1_stems500_2880_manifest(catalog_root: Path) -> ControlledRunManifest:
    """April 2026 15m_PD1 stems[500:2880] — remainder of month (2380 closes, 48×50 batches)."""
    from .historical_batch_config import StrategyCombo

    combo = StrategyCombo("15m", 1)
    month = "2026-04"
    stem_slice = StemSlice(500, 2880)
    all_stems = tuple(iter_stems(catalog_root, month=month, combo_folder=combo.folder))
    stems = stem_slice.resolve(all_stems)
    if len(all_stems) != 2880:
        raise BatchImportError(
            f"April 15m_PD1 catalog stem count {len(all_stems)} != expected 2880"
        )
    run_id = "00000000-0000-4000-8000-000000004c01"
    fp = compute_scope_fingerprint(
        approved_month=month,
        combo_folder=combo.folder,
        pipeline_line_id=combo.pipeline_line_id(),
        stem_slice=stem_slice,
        stems=stems,
    )
    prefix = "phase4o_apr2026_15mpd1_v1"
    batch_count = (len(stems) + 49) // 50
    batch_ids = tuple(f"{prefix}_batch{b:02d}" for b in range(1, batch_count + 1))
    return ControlledRunManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        run_id=run_id,
        approved_month=month,
        combo_folder=combo.folder,
        pipeline_line_id=combo.pipeline_line_id(),
        stem_slice=stem_slice,
        max_snapshots=2380,
        max_batches=48,
        batch_size=50,
        import_batch_id=prefix,
        backup_recovery_id="923fc087-9e6b-47f1-bbaf-37d765b1182a",
        scope_fingerprint=fp,
        batch_import_ids=batch_ids,
    )


def _build_april_full_combo_manifest(
    catalog_root: Path,
    *,
    combo_folder: str,
    pool_detail: int,
    run_id: str,
    import_prefix: str,
    backup_recovery_id: str,
) -> ControlledRunManifest:
    from .historical_batch_config import StrategyCombo

    combo = StrategyCombo("15m" if combo_folder.startswith("15m") else "4h", pool_detail)
    if combo.folder != combo_folder:
        raise BatchImportError(f"combo_folder mismatch {combo_folder} vs {combo.folder}")
    month = "2026-04"
    all_stems = tuple(iter_stems(catalog_root, month=month, combo_folder=combo.folder))
    stem_slice = StemSlice(0, len(all_stems))
    stems = stem_slice.resolve(all_stems)
    fp = compute_scope_fingerprint(
        approved_month=month,
        combo_folder=combo.folder,
        pipeline_line_id=combo.pipeline_line_id(),
        stem_slice=stem_slice,
        stems=stems,
    )
    batch_size = 50
    batch_count = (len(stems) + batch_size - 1) // batch_size
    batch_ids = tuple(f"{import_prefix}_batch{b:02d}" for b in range(1, batch_count + 1))
    return ControlledRunManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        run_id=run_id,
        approved_month=month,
        combo_folder=combo.folder,
        pipeline_line_id=combo.pipeline_line_id(),
        stem_slice=stem_slice,
        max_snapshots=len(stems),
        max_batches=batch_count,
        batch_size=batch_size,
        import_batch_id=import_prefix,
        backup_recovery_id=backup_recovery_id,
        scope_fingerprint=fp,
        batch_import_ids=batch_ids,
    )


def build_phase4r_april_15mpd5_full_manifest(catalog_root: Path) -> ControlledRunManifest:
    """April 2026 full month 15m_PD5 — 2880 closes (58×50 batches, last batch 30)."""
    return _build_april_full_combo_manifest(
        catalog_root,
        combo_folder="15m_PD5",
        pool_detail=5,
        run_id="00000000-0000-4000-8000-000000004d01",
        import_prefix="phase4r_apr2026_15mpd5_v1",
        backup_recovery_id="be78daa2-c816-4b68-b490-75941f2cf82e",
    )


def build_phase4r_april_15mpd10_full_manifest(catalog_root: Path) -> ControlledRunManifest:
    """April 2026 full month 15m_PD10 production line (distinct from Aug pilot ledger)."""
    return _build_april_full_combo_manifest(
        catalog_root,
        combo_folder="15m_PD10",
        pool_detail=10,
        run_id="00000000-0000-4000-8000-000000004e01",
        import_prefix="phase4r_apr2026_15mpd10_v1",
        backup_recovery_id="be78daa2-c816-4b68-b490-75941f2cf82e",
    )


def _build_month_full_combo_manifest(
    catalog_root: Path,
    *,
    approved_month: str,
    combo_folder: str,
    pool_detail: int,
    run_id: str,
    import_prefix: str,
    backup_recovery_id: str,
) -> ControlledRunManifest:
    from .historical_batch_config import StrategyCombo

    combo = StrategyCombo("15m" if combo_folder.startswith("15m") else "4h", pool_detail)
    if combo.folder != combo_folder:
        raise BatchImportError(f"combo_folder mismatch {combo_folder} vs {combo.folder}")
    all_stems = tuple(iter_stems(catalog_root, month=approved_month, combo_folder=combo.folder))
    stem_slice = StemSlice(0, len(all_stems))
    stems = stem_slice.resolve(all_stems)
    fp = compute_scope_fingerprint(
        approved_month=approved_month,
        combo_folder=combo.folder,
        pipeline_line_id=combo.pipeline_line_id(),
        stem_slice=stem_slice,
        stems=stems,
    )
    batch_size = 50
    batch_count = (len(stems) + batch_size - 1) // batch_size
    batch_ids = tuple(f"{import_prefix}_batch{b:02d}" for b in range(1, batch_count + 1))
    return ControlledRunManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        run_id=run_id,
        approved_month=approved_month,
        combo_folder=combo.folder,
        pipeline_line_id=combo.pipeline_line_id(),
        stem_slice=stem_slice,
        max_snapshots=len(stems),
        max_batches=batch_count,
        batch_size=batch_size,
        import_batch_id=import_prefix,
        backup_recovery_id=backup_recovery_id,
        scope_fingerprint=fp,
        batch_import_ids=batch_ids,
    )


MAY_2026_COMBO_RUN_IDS: dict[str, str] = {
    "15m_PD1": "00000000-0000-4000-8000-000000005101",
    "15m_PD5": "00000000-0000-4000-8000-000000005102",
    "15m_PD10": "00000000-0000-4000-8000-000000005103",
    "4h_PD10": "00000000-0000-4000-8000-000000005104",
}


def build_phase4w_may2026_combo_manifest(
    catalog_root: Path,
    combo_folder: str,
    *,
    backup_recovery_id: str,
) -> ControlledRunManifest:
    """May 2026 full month for one strategy combo (frozen catalog stems[0:N])."""
    pool_detail = int(combo_folder.split("_PD")[1])
    prefix = f"phase4w_may2026_{combo_folder.lower()}_v1"
    return _build_month_full_combo_manifest(
        catalog_root,
        approved_month="2026-05",
        combo_folder=combo_folder,
        pool_detail=pool_detail,
        run_id=MAY_2026_COMBO_RUN_IDS[combo_folder],
        import_prefix=prefix,
        backup_recovery_id=backup_recovery_id,
    )


JUNE_2026_COMBO_RUN_IDS: dict[str, str] = {
    "15m_PD1": "00000000-0000-4000-8000-000000005201",
    "15m_PD5": "00000000-0000-4000-8000-000000005202",
    "15m_PD10": "00000000-0000-4000-8000-000000005203",
    "4h_PD10": "00000000-0000-4000-8000-000000005204",
}


def build_phase4y_june2026_combo_manifest(
    catalog_root: Path,
    combo_folder: str,
    *,
    backup_recovery_id: str,
) -> ControlledRunManifest:
    """June 2026 full month for one strategy combo (frozen catalog stems[0:N])."""
    return build_phase4z_month_combo_manifest(
        catalog_root,
        "2026-06",
        combo_folder,
        backup_recovery_id=backup_recovery_id,
        import_prefix="phase4y_jun2026",
    )


PHASE4Z_MONTH_SLUG: dict[str, str] = {
    "2026-06": "jun2026",
    "2026-07": "jul2026",
    "2026-08": "aug2026",
    "2026-09": "sep2026",
}

PHASE4Z_MONTH_RUN_ID_BASE: dict[str, str] = {
    "2026-06": "520",
    "2026-07": "530",
    "2026-08": "540",
    "2026-09": "550",
}

PHASE4Z_COMBO_RUN_SUFFIX: dict[str, int] = {
    "15m_PD1": 1,
    "15m_PD5": 2,
    "15m_PD10": 3,
    "4h_PD10": 4,
}


def phase4z_combo_run_id(approved_month: str, combo_folder: str) -> str:
    base = int(PHASE4Z_MONTH_RUN_ID_BASE[approved_month])
    suffix = PHASE4Z_COMBO_RUN_SUFFIX[combo_folder]
    tail = base * 10 + suffix
    return f"00000000-0000-4000-8000-{tail:012d}"


def build_phase4z_month_combo_manifest(
    catalog_root: Path,
    approved_month: str,
    combo_folder: str,
    *,
    backup_recovery_id: str,
    import_prefix: str | None = None,
) -> ControlledRunManifest:
    """Frozen full-month manifest for Jun–Sep 2026 overnight import."""
    pool_detail = int(combo_folder.split("_PD")[1])
    slug = PHASE4Z_MONTH_SLUG[approved_month]
    prefix = import_prefix or f"phase4z_{slug}_{combo_folder.lower()}_v1"
    return _build_month_full_combo_manifest(
        catalog_root,
        approved_month=approved_month,
        combo_folder=combo_folder,
        pool_detail=pool_detail,
        run_id=phase4z_combo_run_id(approved_month, combo_folder),
        import_prefix=prefix,
        backup_recovery_id=backup_recovery_id,
    )


def write_manifest_json(path: Path, manifest: ControlledRunManifest) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest.canonical_dict(), indent=2) + "\n",
        encoding="utf-8",
    )

"""Deterministic full-catalog import plan (Apr–Sep 2026, four combos) — planning only."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .batch_runner import BatchSpec, make_batch_specs
from .catalog_inventory import inventory_from_coverage, iter_stems
from .historical_batch_config import (
    DEFAULT_BATCH_SIZE_CLOSES,
    MAX_BATCH_SIZE_CLOSES,
    MONTHS_APR_SEP,
    PILOT_COMBO_FOLDER,
    PILOT_DECISION_CLOSE_STEMS,
    PILOT_MONTH,
    STRATEGY_COMBOS,
)


@dataclass(frozen=True)
class ImportPlanSummary:
    catalog_snapshot_count: int
    batches: int
    stems_total: int
    already_imported_meta_estimate: int
    remaining_meta_writes_estimate: int
    pilot_stems: int
    phase4f_april_4h_stems: int


def iter_all_batch_specs(
    catalog_root: Path,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE_CLOSES,
    import_batch_id_prefix: str = "historical_mixed_frozen",
) -> Iterator[BatchSpec]:
    """
    Yield batch specs in causal order: month ascending, combo folder stable order, stems sorted.

    Each batch gets a deterministic ``import_batch_id``:
    ``{prefix}_{month}_{combo}_{firstStem}_{lastStem}``.
    """
    if batch_size < 1 or batch_size > MAX_BATCH_SIZE_CLOSES:
        raise ValueError(f"batch_size must be 1..{MAX_BATCH_SIZE_CLOSES}")
    for month in MONTHS_APR_SEP:
        for combo in STRATEGY_COMBOS:
            stems = tuple(iter_stems(catalog_root, month=month, combo_folder=combo.folder))
            if not stems:
                continue
            for i in range(0, len(stems), batch_size):
                chunk = stems[i : i + batch_size]
                bid = f"{import_batch_id_prefix}_{month}_{combo.folder}_{chunk[0]}_{chunk[-1]}"
                yield BatchSpec(
                    month=month,
                    combo_folder=combo.folder,
                    stems=chunk,
                    pipeline_line_id=combo.pipeline_line_id(),
                    import_batch_id=bid,
                )


def build_full_import_plan(
    catalog_root: Path,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE_CLOSES,
    phase4f_batch_id: str = "phase4f_first_50_apr2026_4hpd10_v1",
    phase4f_stem_count: int = 50,
) -> dict[str, Any]:
    inv = inventory_from_coverage(catalog_root)
    catalog_n = int(inv.get("manifest_snapshot_count") or inv.get("coverage_total_found") or 0)
    specs = list(iter_all_batch_specs(catalog_root, batch_size=batch_size))
    stems_total = sum(len(s.stems) for s in specs)
    pilot_n = len(PILOT_DECISION_CLOSE_STEMS)
    # Meta already in CH: pilot (10 global) + phase4f april 4h (50 global) — documented constants.
    imported_meta = pilot_n + phase4f_stem_count
    return {
        "catalog_snapshot_count": catalog_n,
        "stems_in_plan": stems_total,
        "batch_count": len(specs),
        "batch_size": batch_size,
        "combos": [c.folder for c in STRATEGY_COMBOS],
        "months": list(MONTHS_APR_SEP),
        "pilot_overlap": {
            "month": PILOT_MONTH,
            "combo": PILOT_COMBO_FOLDER,
            "stems": list(PILOT_DECISION_CLOSE_STEMS),
            "note": "meta/member/catalog exist; production line may need publish-only overlap handling",
        },
        "phase4f_completed": {
            "import_batch_id": phase4f_batch_id,
            "combo": "4h_PD10",
            "month": "2026-04",
            "stems": phase4f_stem_count,
        },
        "remaining_meta_writes_estimate": max(0, catalog_n - imported_meta),
        "batches": [
            {
                "batch_id": s.batch_id,
                "import_batch_id": s.import_batch_id,
                "pipeline_line_id": s.pipeline_line_id,
                "month": s.month,
                "combo_folder": s.combo_folder,
                "stem_count": len(s.stems),
                "first_stem": s.stems[0],
                "last_stem": s.stems[-1],
            }
            for s in specs
        ],
    }


def estimate_batches_for_sizes(catalog_root: Path) -> dict[str, Any]:
    """Dry-run batch counts for 50 vs 200 (no ClickHouse)."""
    out = {}
    for size in (50, 100, 200):
        if size > MAX_BATCH_SIZE_CLOSES:
            continue
        specs = list(iter_all_batch_specs(catalog_root, batch_size=size))
        out[str(size)] = {"batch_count": len(specs), "max_stems_per_batch": size}
    return out

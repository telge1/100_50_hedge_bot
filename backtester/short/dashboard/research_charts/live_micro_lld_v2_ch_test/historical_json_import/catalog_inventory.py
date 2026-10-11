"""Read-only inventory of the released mixed-frozen catalog."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .historical_batch_config import MONTHS_APR_SEP, STRATEGY_COMBOS, StrategyCombo
from .parse import snapshot_path_for_stem


@dataclass(frozen=True)
class ComboMonthSlice:
    combo: StrategyCombo
    month: str
    expected: int
    found: int
    missing: int
    duplicates: int
    invalid: int
    status: str
    first_close: str
    last_close: str


def load_coverage_matrix(catalog_root: Path) -> list[dict[str, Any]]:
    path = catalog_root / "production" / "coverage_matrix.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data.get("MONTH_TF_PD_COVERAGE") or [])


def inventory_from_coverage(catalog_root: Path) -> dict[str, Any]:
    rows = load_coverage_matrix(catalog_root)
    manifest = json.loads(
        (catalog_root / "production" / "catalog_manifest.json").read_text(encoding="utf-8")
    )
    by_combo: dict[str, int] = defaultdict(int)
    slices: list[ComboMonthSlice] = []
    total_found = 0
    for row in rows:
        combo_name = str(row["combo"])
        found = int(row["FOUND"])
        by_combo[combo_name] += found
        total_found += found
        sc = next((c for c in STRATEGY_COMBOS if c.folder == combo_name), None)
        if sc is None:
            continue
        slices.append(
            ComboMonthSlice(
                combo=sc,
                month=str(row["month"]),
                expected=int(row["EXPECTED"]),
                found=found,
                missing=int(row["MISSING"]),
                duplicates=int(row["DUPLICATES"]),
                invalid=int(row["INVALID"]),
                status=str(row["STATUS"]),
                first_close=str(row["FIRST_CLOSE"]),
                last_close=str(row["LAST_CLOSE"]),
            )
        )
    disk_counts = count_json_files_on_disk(catalog_root)
    return {
        "catalog_id": manifest.get("catalog_id"),
        "manifest_snapshot_count": int(manifest.get("snapshot_count") or 0),
        "coverage_total_found": total_found,
        "disk_total_json": sum(disk_counts.values()),
        "by_combo_coverage": dict(sorted(by_combo.items())),
        "by_combo_disk": disk_counts,
        "slices": [
            {
                "combo_folder": s.combo.folder,
                "month": s.month,
                "expected": s.expected,
                "found": s.found,
                "missing": s.missing,
                "duplicates": s.duplicates,
                "invalid": s.invalid,
                "status": s.status,
                "first_close": s.first_close,
                "last_close": s.last_close,
            }
            for s in slices
        ],
        "all_slices_pass": all(s.status == "PASS" for s in slices),
    }


def count_json_files_on_disk(catalog_root: Path) -> dict[str, int]:
    results_root = catalog_root / "production" / "results"
    out: dict[str, int] = defaultdict(int)
    if not results_root.is_dir():
        return {}
    for month_dir in results_root.iterdir():
        if not month_dir.is_dir():
            continue
        for combo_dir in month_dir.iterdir():
            if combo_dir.is_dir():
                out[combo_dir.name] += sum(1 for _ in combo_dir.glob("*.json"))
    return dict(sorted(out.items()))


def iter_stems(
    catalog_root: Path,
    *,
    month: str,
    combo_folder: str,
) -> Iterator[str]:
    combo_dir = catalog_root / "production" / "results" / month / combo_folder
    if not combo_dir.is_dir():
        return iter(())
    return iter(sorted(p.stem for p in combo_dir.glob("*.json")))


def iter_all_stems(catalog_root: Path) -> Iterator[tuple[str, str, str]]:
    """Yield (month, combo_folder, stem)."""
    for month in MONTHS_APR_SEP:
        for combo in STRATEGY_COMBOS:
            for stem in iter_stems(catalog_root, month=month, combo_folder=combo.folder):
                yield month, combo.folder, stem


def stem_path(catalog_root: Path, month: str, combo_folder: str, stem: str) -> Path:
    return snapshot_path_for_stem(
        catalog_root, month=month, combo=combo_folder, stem=stem
    )

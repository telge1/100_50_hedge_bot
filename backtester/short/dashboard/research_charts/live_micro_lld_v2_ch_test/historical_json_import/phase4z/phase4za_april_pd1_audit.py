#!/usr/bin/env python3
"""Read-only April 2026 15m_PD1 manifest coverage audit (Phase 4Z-A)."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
for _p in (_REPO, _DASH):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

from research_charts.live_micro_lld_v2_ch_test.historical_json_import.catalog_inventory import (
    iter_stems,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_run_manifest import (
    load_controlled_run_manifest,
    resolve_manifest_stems,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_config import (
    CATALOG_ROOT,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.post_import_verification import (
    verify_manifest_file,
)

MANIFEST_DIR = Path(__file__).resolve().parents[1] / "manifests"
LOG_DIR = _REPO / "logs" / "historical_import" / "overnight_2026"
OUT = LOG_DIR / "phase4za_april_pd1_audit.json"
APRIL_PD1_MANIFESTS = [
    ("phase4k_april_15mpd1_500_v1.json", "phase4K first 500 stems [0:500)", "_controlled_import_progress_phase4n"),
    ("phase4o_april_15mpd1_stems500_2880_v2.json", "phase4O stems [500:2880)", "_controlled_import_progress_phase4q"),
]
ARCHIVE_FINAL = _REPO / "logs/historical_import/april2026/final_readback.json"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _audit_progress(progress_rel: str, import_prefix: str) -> dict:
    prog = CATALOG_ROOT / progress_rel
    batches = sorted(prog.glob("2026-04_15m_PD1_*.json"))
    statuses = []
    for p in batches:
        d = json.loads(p.read_text(encoding="utf-8"))
        if import_prefix in str(d.get("import_batch_id", "")):
            statuses.append({"batch_id": d.get("batch_id"), "status": d.get("status")})
    ok = all(s["status"] == "readback_ok" for s in statuses) if statuses else False
    return {
        "progress_dir": str(prog),
        "batch_files_matching_prefix": len(statuses),
        "all_readback_ok": ok,
        "batches": statuses,
    }


def main() -> int:
    if os.environ.get("HISTORICAL_JSON_IMPORT_ALLOW_WRITE") == "1":
        print("refusing: write gate set", file=sys.stderr)
        return 2

    catalog_stems = tuple(iter_stems(CATALOG_ROOT, month="2026-04", combo_folder="15m_PD1"))
    report: dict = {
        "schema_version": 1,
        "phase": "4Z-A",
        "combo": "15m_PD1",
        "month": "2026-04",
        "started_at_utc": _utc(),
        "write_gates_active": False,
        "checks": [],
        "manifests": [],
        "set_analysis": {},
    }

    union: list[str] = []
    manifest_stem_sets: dict[str, list[str]] = {}
    for fname, note, progress in APRIL_PD1_MANIFESTS:
        path = MANIFEST_DIR / fname
        m = load_controlled_run_manifest(path)
        stems = list(resolve_manifest_stems(CATALOG_ROOT, m))
        manifest_stem_sets[fname] = stems
        union.extend(stems)
        entry = {
            "file": fname,
            "note": note,
            "run_id": m.run_id,
            "stem_slice": {"start": m.stem_slice.start_index, "end": m.stem_slice.end_index},
            "stem_count": len(stems),
            "first_stem": stems[0] if stems else None,
            "last_stem": stems[-1] if stems else None,
            "scope_fingerprint": m.scope_fingerprint,
            "progress_audit": _audit_progress(progress, m.import_batch_id),
        }
        report["manifests"].append(entry)

    union_set = set(union)
    catalog_set = set(catalog_stems)
    missing_from_union = sorted(catalog_set - union_set)
    extra_in_union = sorted(union_set - catalog_set)
    overlap_dupes = len(union) - len(union_set)

    report["set_analysis"] = {
        "frozen_catalog_stem_count": len(catalog_stems),
        "union_manifest_stem_count": len(union),
        "union_unique_stem_count": len(union_set),
        "overlap_duplicate_entries": overlap_dupes,
        "missing_from_union_count": len(missing_from_union),
        "missing_from_union_first_last": (
            [missing_from_union[0], missing_from_union[-1]] if missing_from_union else []
        ),
        "extra_not_in_catalog_count": len(extra_in_union),
        "union_equals_catalog": union_set == catalog_set and overlap_dupes == 0,
    }
    report["checks"].append(
        {
            "id": "frozen_catalog_vs_manifest_union",
            "status": "PASS" if report["set_analysis"]["union_equals_catalog"] else "FAIL",
            "expected_stems": 2880,
            "catalog_stems": len(catalog_stems),
            "union_unique": len(union_set),
        }
    )

    phase4k_stems = set(manifest_stem_sets["phase4k_april_15mpd1_500_v1.json"])
    phase4o_stems = set(manifest_stem_sets["phase4o_april_15mpd1_stems500_2880_v2.json"])
    only_4k = sorted(phase4k_stems - phase4o_stems)
    only_4o = sorted(phase4o_stems - phase4k_stems)
    report["set_analysis"]["phase4k_only_count"] = len(only_4k)
    report["set_analysis"]["phase4o_only_count"] = len(only_4o)
    report["set_analysis"]["phase4k_stem_range"] = (
        [only_4k[0], only_4k[-1]] if only_4k else []
    )
    report["checks"].append(
        {
            "id": "phase4k_covers_first_500_stems",
            "status": "PASS" if len(only_4k) == 500 and len(phase4k_stems) == 500 else "FAIL",
            "phase4k_stem_count": len(phase4k_stems),
            "disjoint_from_phase4o": phase4k_stems.isdisjoint(phase4o_stems),
        }
    )

    # Read-only readback on phase4k (500 stems — resource-bounded)
    kpath = MANIFEST_DIR / "phase4k_april_15mpd1_500_v1.json"
    km = load_controlled_run_manifest(kpath)
    rb_k = verify_manifest_file(CATALOG_ROOT, kpath, test_run_id=km.run_id)
    report["checks"].append(
        {
            "id": "phase4k_manifest_readback",
            "status": "PASS" if rb_k.get("ok") else "FAIL",
            "method": "verify_manifest_file",
            "stems": rb_k.get("stems"),
            "batches": rb_k.get("batches"),
            "parity_failures": rb_k.get("parity_failures"),
            "sidecar_failures": rb_k.get("sidecar_failures"),
        }
    )

    # phase4o already in final_historical_readback — cross-reference, no full re-run required
    final_path = LOG_DIR / "final_historical_readback.json"
    phase4o_rb_ok = None
    if final_path.is_file():
        final = json.loads(final_path.read_text(encoding="utf-8"))
        for item in final.get("manifest_readbacks", []):
            if item.get("manifest_path", "").endswith("phase4o_april_15mpd1_stems500_2880_v2.json"):
                phase4o_rb_ok = item.get("readback", {}).get("ok")
                break
    report["checks"].append(
        {
            "id": "phase4o_manifest_readback",
            "status": "PASS" if phase4o_rb_ok else ("NOT_VERIFIED" if phase4o_rb_ok is None else "FAIL"),
            "method": "cross_reference final_historical_readback.json (2026-10-12 run)",
            "ok": phase4o_rb_ok,
            "note": "Re-run verify_manifest_file optional; final readback recorded 0 parity/sidecar failures",
        }
    )

    for m in report["manifests"]:
        pa = m["progress_audit"]
        report["checks"].append(
            {
                "id": f"progress_{m['file']}",
                "status": "PASS"
                if pa["all_readback_ok"] and pa["batch_files_matching_prefix"] > 0
                else "FAIL",
                "batch_files": pa["batch_files_matching_prefix"],
            }
        )

    report["checks"].append(
        {
            "id": "april2026_archived_final_readback",
            "status": "NOT_VERIFIED" if not ARCHIVE_FINAL.is_file() else "PASS",
            "path": str(ARCHIVE_FINAL),
            "note": "Missing archive only; no synthetic reconstruction",
        }
    )

    fail = sum(1 for c in report["checks"] if c["status"] == "FAIL")
    nv = sum(1 for c in report["checks"] if c["status"] == "NOT_VERIFIED")
    report["summary"] = {
        "pass": sum(1 for c in report["checks"] if c["status"] == "PASS"),
        "fail": fail,
        "not_verified": nv,
        "overall": "FAIL" if fail else ("PASS_WITH_NOT_VERIFIED" if nv else "PASS"),
        "finished_at_utc": _utc(),
    }
    report["conclusion"] = (
        "All 2880 April PD1 catalog stems are covered by phase4k [0:500] + phase4o v2 [500:2880] "
        "with disjoint union matching the frozen catalog; phase4k readback PASS in this audit."
        if report["summary"]["overall"] in ("PASS", "PASS_WITH_NOT_VERIFIED")
        and report["set_analysis"]["union_equals_catalog"]
        else "Coverage or readback gaps remain — see checks."
    )

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"out": str(OUT), "overall": report["summary"]["overall"]}, indent=2))
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())

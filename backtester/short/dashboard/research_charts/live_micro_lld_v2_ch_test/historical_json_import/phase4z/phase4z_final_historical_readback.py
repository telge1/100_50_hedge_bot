#!/usr/bin/env python3
"""
Read-only Apr–Sep 2026 final historical validation (no CH writes).

Uses ``post_import_verification.verify_manifest_file`` per production manifest.
"""

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
    inventory_from_coverage,
    iter_stems,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.ch_import_state import (
    ch_client_and_db,
    fetch_published_snapshot_ids,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_run_manifest import (
    _build_month_full_combo_manifest,
    load_controlled_run_manifest,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.historical_batch_config import (
    PILOT_PIPELINE_LINE_ID,
    STRATEGY_COMBOS,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.orphan_audit import (
    audit_orphan_members,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_config import (
    CATALOG_ROOT,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_overlap import (
    pilot_snapshot_ids_from_catalog,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.post_import_verification import (
    verify_manifest_file,
    verify_manifest_scope_readback,
)

MANIFEST_DIR = Path(__file__).resolve().parents[1] / "manifests"
LOG_DIR = _REPO / "logs" / "historical_import" / "overnight_2026"
OUT_PATH = LOG_DIR / "final_historical_readback.json"
COMBO_ORDER = ("15m_PD1", "15m_PD5", "15m_PD10", "4h_PD10")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _status(ok: bool, verified: bool = True) -> str:
    if not verified:
        return "NOT_VERIFIED"
    return "PASS" if ok else "FAIL"


def _load_plan_manifests(plan_path: Path) -> list[tuple[str, str, Path]]:
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    month = str(data["approved_month"])
    out: list[tuple[str, str, Path]] = []
    for entry in data["manifests"]:
        combo = str(entry["combo_folder"])
        out.append((month, combo, Path(entry["manifest_path"])))
    return out


def _april_manifest_entries() -> list[tuple[str, str, Path | None, str]]:
    """(month, combo, path or None for ephemeral, note)"""
    return [
        (
            "2026-04",
            "15m_PD1",
            MANIFEST_DIR / "phase4o_april_15mpd1_stems500_2880_v2.json",
            "phase4o production full-month PD1 manifest v2",
        ),
        (
            "2026-04",
            "15m_PD5",
            MANIFEST_DIR / "phase4r_april_15mpd5_full_v2.json",
            "phase4r production full-month PD5 manifest v2",
        ),
        (
            "2026-04",
            "15m_PD10",
            MANIFEST_DIR / "phase4r_april_15mpd10_full_v2.json",
            "phase4r production full-month PD10 manifest v2 (phase4V scope)",
        ),
        (
            "2026-04",
            "4h_PD10",
            None,
            "ephemeral full-month manifest from frozen catalog (April 4h imported in phased batches 4F/4H/4I)",
        ),
    ]


def main() -> int:
    if os.environ.get("HISTORICAL_JSON_IMPORT_ALLOW_WRITE") == "1":
        print("refusing: HISTORICAL_JSON_IMPORT_ALLOW_WRITE=1", file=sys.stderr)
        return 2

    report: dict = {
        "schema_version": 1,
        "phase": "4Z_final_historical_readback",
        "symbol": "DOGEUSDT",
        "window_months": ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"],
        "started_at_utc": _utc(),
        "method": "post_import_verification.verify_manifest_file (read-only CH readback gates)",
        "write_gates_active": False,
        "checks": [],
        "manifest_readbacks": [],
        "artifacts": {
            "overnight_plan": str(MANIFEST_DIR / "june_sep_2026_overnight_import_plan_v1.json"),
            "preflight": str(LOG_DIR / "phase4z_preflight_all_months.json"),
            "resume_reconcile": str(LOG_DIR / "phase4z_resume_reconcile.json"),
            "month_reports": {
                m: str(LOG_DIR / f"{m}_report.json") for m in ("june", "july", "august", "september")
            },
            "may_final_readback": str(_REPO / "logs/historical_import/may2026/final_readback.json"),
        },
    }

    # Coverage matrix (frozen catalog, read-only)
    cov = inventory_from_coverage(CATALOG_ROOT)
    cov_by: dict[tuple[str, str], dict] = {}
    for s in cov["slices"]:
        cov_by[(s["month"], s["combo_folder"])] = s
    cov_rows = []
    cov_ok = True
    for month in report["window_months"]:
        for combo in COMBO_ORDER:
            row = cov_by.get((month, combo))
            if not row:
                cov_ok = False
                cov_rows.append({"month": month, "combo": combo, "status": "MISSING_SLICE"})
                continue
            ok = row.get("status") == "PASS" and int(row.get("missing") or 0) == 0
            if not ok:
                cov_ok = False
            cov_rows.append(
                {
                    "month": month,
                    "combo": combo,
                    "expected": row.get("expected"),
                    "found": row.get("found"),
                    "missing": row.get("missing"),
                    "duplicates": row.get("duplicates"),
                    "first_close": row.get("first_close"),
                    "last_close": row.get("last_close"),
                    "status": _status(ok),
                }
            )
    report["checks"].append(
        {
            "id": "frozen_catalog_coverage_matrix",
            "status": _status(cov_ok),
            "source": str(CATALOG_ROOT / "production/coverage_matrix.json"),
            "rows": cov_rows,
        }
    )

    entries: list[tuple[str, str, Path | None, str, str | None]] = []
    for month, combo, path, note in _april_manifest_entries():
        entries.append((month, combo, path, note, None))
    entries.extend(
        (m, c, p, "phase4w may manifest", None)
        for m, c, p in _load_plan_manifests(MANIFEST_DIR / "phase4w_may2026_monthly_run_plan_v1.json")
    )
    for plan_name, month_slug in (
        ("phase4z_jun2026_monthly_run_plan_v1.json", "june"),
        ("phase4z_jul2026_monthly_run_plan_v1.json", "july"),
        ("phase4z_aug2026_monthly_run_plan_v1.json", "august"),
        ("phase4z_sep2026_monthly_run_plan_v1.json", "september"),
    ):
        entries.extend(
            (m, c, p, f"phase4z {month_slug}", month_slug)
            for m, c, p in _load_plan_manifests(MANIFEST_DIR / plan_name)
        )

    readback_failures = 0
    for month, combo, mpath, note, month_slug in entries:
        item: dict = {
            "month": month,
            "combo_folder": combo,
            "manifest_note": note,
            "manifest_path": str(mpath) if mpath else None,
        }
        try:
            if mpath is not None:
                manifest = load_controlled_run_manifest(mpath)
                rb = verify_manifest_file(CATALOG_ROOT, mpath, test_run_id=manifest.run_id)
            else:
                # April 4h full month — same stem scope as production import, read-only readback
                manifest = _build_month_full_combo_manifest(
                    CATALOG_ROOT,
                    approved_month=month,
                    combo_folder=combo,
                    pool_detail=10,
                    run_id="00000000-0000-4000-8000-000000004101",
                    import_prefix="phase4z_final_readback_apr2026_4h",
                    backup_recovery_id="read-only-final-readback",
                )
                rb = verify_manifest_scope_readback(CATALOG_ROOT, manifest)
                rb["manifest_path"] = "(ephemeral full-month 2026-04 4h_PD10)"
                rb["scope_fingerprint"] = manifest.scope_fingerprint
            item["readback"] = {
                "stems": rb.get("stems"),
                "batches": rb.get("batches"),
                "parity_failures": rb.get("parity_failures"),
                "sidecar_failures": rb.get("sidecar_failures"),
                "ok": rb.get("ok"),
            }
            item["status"] = _status(bool(rb.get("ok")))
            if not rb.get("ok"):
                readback_failures += 1
            if month_slug:
                stored = LOG_DIR / f"{month_slug}_report.json"
                if stored.is_file():
                    stored_rb = json.loads(stored.read_text(encoding="utf-8"))
                    combo_rb = next(
                        (
                            c
                            for c in stored_rb.get("readback", {}).get("combos", [])
                            if c.get("scope_fingerprint") == rb.get("scope_fingerprint")
                            or c.get("manifest_path", "").endswith(mpath.name if mpath else "")
                        ),
                        None,
                    )
                    if combo_rb is None and mpath:
                        combo_rb = next(
                            (
                                c
                                for c in stored_rb.get("readback", {}).get("combos", [])
                                if mpath.name in str(c.get("manifest_path", ""))
                            ),
                            None,
                        )
                    item["stored_month_report_ok"] = combo_rb.get("ok") if combo_rb else None
                    item["stored_month_report_match"] = (
                        combo_rb.get("ok") == rb.get("ok") if combo_rb else None
                    )
        except Exception as exc:  # noqa: BLE001
            item["status"] = "FAIL"
            item["error"] = repr(exc)
            readback_failures += 1
        report["manifest_readbacks"].append(item)

    report["checks"].append(
        {
            "id": "manifest_scope_readback_all_months",
            "status": _status(readback_failures == 0),
            "segments": len(report["manifest_readbacks"]),
            "failures": readback_failures,
        }
    )

    # Orphans (read-only)
    client, db = ch_client_and_db()
    try:
        orphans = audit_orphan_members(client, db)
        pilot_pub = fetch_published_snapshot_ids(client, db, pipeline_line_id=PILOT_PIPELINE_LINE_ID)
        line_counts: dict[str, int] = {}
        for combo in STRATEGY_COMBOS:
            line = combo.pipeline_line_id()
            line_counts[combo.folder] = len(fetch_published_snapshot_ids(client, db, pipeline_line_id=line))
    finally:
        client.close()

    pilot_ids = pilot_snapshot_ids_from_catalog(CATALOG_ROOT)
    report["checks"].append(
        {
            "id": "orphan_member_snapshots",
            "status": _status(len(orphans) == 0),
            "count": len(orphans),
            "sample": orphans[:5],
        }
    )
    report["checks"].append(
        {
            "id": "august_pilot_line",
            "status": _status(len(pilot_pub) == 10 and pilot_pub >= pilot_ids),
            "expected_pilot_ids": len(pilot_ids),
            "publish_on_pilot_line": len(pilot_pub),
            "pilot_ids_subset_of_publish": pilot_ids <= pilot_pub,
        }
    )

    expected_publish: dict[str, int] = {}
    for month in report["window_months"]:
        for combo in COMBO_ORDER:
            row = cov_by.get((month, combo))
            if row:
                line = next(c for c in STRATEGY_COMBOS if c.folder == combo).pipeline_line_id()
                expected_publish[line] = expected_publish.get(line, 0) + int(row.get("found") or 0)

    publish_rows = []
    publish_ok = True
    for combo in STRATEGY_COMBOS:
        line = combo.pipeline_line_id()
        exp = expected_publish.get(line, 0)
        act = line_counts.get(combo.folder, 0)
        # Production lines: cumulative publish per line across Apr–Sep scopes
        ok = act == exp
        if not ok:
            publish_ok = False
        publish_rows.append(
            {
                "combo_folder": combo.folder,
                "pipeline_line_id": line,
                "expected_publish_snapshots_apr_sep": exp,
                "actual_publish_on_line": act,
                "status": _status(ok),
            }
        )
    report["checks"].append(
        {
            "id": "production_publish_line_counts_apr_sep",
            "status": _status(publish_ok),
            "note": "Sum of frozen-catalog snapshot counts per combo line Apr–Sep (pilot line excluded)",
            "rows": publish_rows,
        }
    )

    # May stored final readback cross-check (artifact only — not re-run substitute)
    may_path = Path(report["artifacts"]["may_final_readback"])
    may_ok = False
    if may_path.is_file():
        may_data = json.loads(may_path.read_text(encoding="utf-8"))
        may_ok = all(c.get("ok") for c in may_data.get("combos", []))
    report["checks"].append(
        {
            "id": "may2026_stored_final_readback_artifact",
            "status": _status(may_ok, verified=may_path.is_file()),
            "path": str(may_path),
            "note": "April has no equivalent stored final_readback.json in logs; Apr verified live in this run",
        }
    )

    # April stored artifact
    report["checks"].append(
        {
            "id": "april2026_stored_final_readback_artifact",
            "status": "NOT_VERIFIED",
            "path": None,
            "note": "No logs/historical_import/april2026/final_readback.json found; Apr combos verified live except phased-import note on 4h",
        }
    )

    fail = sum(1 for c in report["checks"] if c["status"] == "FAIL")
    nver = sum(1 for c in report["checks"] if c["status"] == "NOT_VERIFIED")
    pass_n = sum(1 for c in report["checks"] if c["status"] == "PASS")
    report["summary"] = {
        "pass": pass_n,
        "fail": fail,
        "not_verified": nver,
        "overall": "PASS" if fail == 0 and nver == 0 else ("FAIL" if fail else "PASS_WITH_NOT_VERIFIED"),
        "finished_at_utc": _utc(),
    }
    report["recommendation"] = (
        "Freigabe nächste Phase: Datenbasis Apr–Sep erscheint import-vollständig und readback-konsistent."
        if report["summary"]["overall"] == "PASS"
        else (
            "STOP: Behebe FAIL-Prüfungen vor Freigabe."
            if fail
            else "Eingeschränkte Freigabe: NOT_VERIFIED-Punkte dokumentieren (v. a. April-Artefakt-Nachweis)."
        )
    )

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"ok": fail == 0, "out": str(OUT_PATH), "overall": report["summary"]["overall"]}, indent=2))
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Read-only reconcile + resume checkpoint alignment for Phase 4Z resume."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
for _p in (_REPO, _DASH):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_runner import (
    validate_batch,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.ch_import_state import (
    ch_client_and_db,
    read_ch_import_preflight,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_monthly_orchestrator import (
    ControlledMonthlyImportOrchestrator,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_run_manifest import (
    iter_manifest_batch_specs,
    load_controlled_run_manifest,
    resolve_manifest_stems,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.orphan_audit import (
    audit_orphan_members,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_config import (
    CATALOG_ROOT,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.productive_prewrite_gate import (
    assess_scope_ch_states,
    assert_scope_recovery_safe,
    load_scope_layer_map_for_snapshots,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_overlap import (
    pilot_snapshot_ids_from_catalog,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.ch_import_state import (
    fetch_published_snapshot_ids,
)

PROGRESS = CATALOG_ROOT / "_controlled_import_progress_phase4z_overnight"
LOG = _REPO / "logs" / "historical_import" / "overnight_2026"
MANIFEST_PD10 = (
    _DASH
    / "research_charts/live_micro_lld_v2_ch_test/historical_json_import/manifests"
    / "phase4z_aug2026_15m_pd10_full_v1.json"
)
EXPECTED = {
    "2026-06": {"15m_PD1": 58, "15m_PD5": 58, "15m_PD10": 58, "4h_PD10": 4},
    "2026-07": {"15m_PD1": 60, "15m_PD5": 60, "15m_PD10": 60, "4h_PD10": 4},
    "2026-08": {"15m_PD1": 60, "15m_PD5": 60, "15m_PD10": 60, "4h_PD10": 4},
    "2026-09": {"15m_PD1": 58, "15m_PD5": 58, "15m_PD10": 58, "4h_PD10": 4},
}
FOLDER = {"2026-06": "june", "2026-07": "july", "2026-08": "august", "2026-09": "september"}


def _count_batches(month_key: str, combo: str) -> tuple[int, str | None]:
    folder = FOLDER[month_key]
    cdir = PROGRESS / folder / combo
    if not cdir.is_dir():
        return 0, None
    batches = sorted(cdir.glob("2026-*.json"))
    if not batches:
        return 0, None
    last = batches[-1]
    d = json.loads(last.read_text(encoding="utf-8"))
    return len(batches), d.get("status")


def main() -> int:
    out: dict = {"ok": False, "checks": []}

    # batch_write pilot wiring
    bw = Path(__file__).resolve().parents[1] / "batch_write.py"
    text = bw.read_text(encoding="utf-8")
    if "pilot_snapshot_ids=pilot_ids" not in text or "pilot_snapshot_ids_from_catalog" not in text:
        out["checks"].append({"check": "batch_write_pilot_gate", "ok": False})
        LOG.mkdir(parents=True, exist_ok=True)
        (LOG / "phase4z_resume_reconcile.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        print(json.dumps(out, indent=2))
        return 1
    out["checks"].append({"check": "batch_write_pilot_gate", "ok": True})

    for month_key, combos in EXPECTED.items():
        folder = FOLDER[month_key]
        for combo, exp in combos.items():
            n, st = _count_batches(month_key, combo)
            if month_key in ("2026-06", "2026-07"):
                ok = n == exp and st == "readback_ok"
            elif month_key == "2026-08" and combo in ("15m_PD1", "15m_PD5"):
                ok = n == exp and st == "readback_ok"
            elif month_key == "2026-08" and combo == "15m_PD10":
                ok = n == 1 and st == "readback_ok"
            elif month_key == "2026-08" and combo == "4h_PD10":
                ok = n == 0
            else:
                ok = n == 0
            out["checks"].append(
                {
                    "check": f"batches_{month_key}_{combo}",
                    "ok": ok,
                    "count": n,
                    "expected": exp,
                    "last_status": st,
                }
            )
            if not ok:
                print(json.dumps(out, indent=2))
                (LOG / "phase4z_resume_reconcile.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
                return 1

    batch1 = PROGRESS / "august" / "15m_PD10" / "2026-08_15m_PD10_20260801T000000Z_20260801T121500Z.json"
    b1 = json.loads(batch1.read_text(encoding="utf-8"))
    if b1.get("status") != "readback_ok" or b1.get("import_batch_id") != "phase4z_aug2026_15m_pd10_v1_batch01":
        out["checks"].append({"check": "aug_pd10_batch01", "ok": False, "detail": b1.get("status")})
        (LOG / "phase4z_resume_reconcile.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        return 1
    out["checks"].append({"check": "aug_pd10_batch01", "ok": True})

    manifest = load_controlled_run_manifest(MANIFEST_PD10)
    stems = resolve_manifest_stems(CATALOG_ROOT, manifest)
    specs = iter_manifest_batch_specs(CATALOG_ROOT, manifest, stems)
    if len(specs) < 2:
        out["checks"].append({"check": "batch02_exists", "ok": False})
        return 1
    spec2 = specs[1]
    vr2 = validate_batch(CATALOG_ROOT, spec2)
    if vr2.get("errors"):
        out["checks"].append({"check": "batch02_validate", "ok": False, "errors": vr2["errors"]})
        return 1
    pilot_ids = pilot_snapshot_ids_from_catalog(CATALOG_ROOT)
    layer_map = load_scope_layer_map_for_snapshots(vr2.get("snapshot_ids") or [])
    client, db = ch_client_and_db()
    try:
        published = fetch_published_snapshot_ids(client, db, pipeline_line_id=manifest.pipeline_line_id)
        orphans = audit_orphan_members(client, db)
    finally:
        client.close()
    scope = assess_scope_ch_states(
        vr2.get("snapshot_ids") or [],
        pipeline_line_id=manifest.pipeline_line_id,
        published_on_line=published,
        layer_by_snapshot=layer_map,
        pilot_snapshot_ids=pilot_ids,
    )
    try:
        assert_scope_recovery_safe(scope)
        gate_ok = True
        gate_err = ""
    except Exception as exc:  # noqa: BLE001
        gate_ok = False
        gate_err = repr(exc)
    out["checks"].append(
        {
            "check": "batch02_prewrite_scope",
            "ok": gate_ok,
            "batch_id": spec2.batch_id,
            "error": gate_err,
            "pilot_in_batch": sorted(set(vr2.get("snapshot_ids") or []) & pilot_ids),
        }
    )
    orphan_n = len(orphans)
    out["checks"].append(
        {"check": "orphan_members", "ok": orphan_n == 0, "count": orphan_n, "sample": orphans[:3]}
    )
    if not gate_ok or orphan_n != 0:
        (LOG / "phase4z_resume_reconcile.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        print(json.dumps(out, indent=2))
        return 1

    # Align orchestrator checkpoint (progress-only; matches existing batch01 file)
    run_state_path = PROGRESS / "august" / "15m_PD10" / f"{manifest.run_id}.json"
    template_path = PROGRESS / "august" / "15m_PD1" / "00000000-0000-4000-8000-000000005401.json"
    template = json.loads(template_path.read_text(encoding="utf-8"))
    checkpoint = {
        "run_id": manifest.run_id,
        "scope_fingerprint": manifest.scope_fingerprint,
        "mode": "write_and_readback",
        "batches_planned": manifest.max_batches,
        "batches_completed": 1,
        "test_run_id": manifest.run_id,
        "status": "in_progress",
        "errors": [],
        "backup_provenance": template["backup_provenance"],
        "finished_at": b1.get("finished_at"),
        "resume_aligned_from": str(batch1.name),
    }
    if run_state_path.is_file():
        prior = json.loads(run_state_path.read_text(encoding="utf-8"))
        if int(prior.get("batches_completed") or 0) != 1:
            out["checks"].append(
                {
                    "check": "pd10_run_state",
                    "ok": False,
                    "detail": f"unexpected batches_completed={prior.get('batches_completed')}",
                }
            )
            (LOG / "phase4z_resume_reconcile.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
            return 1
    else:
        run_state_path.write_text(json.dumps(checkpoint, indent=2) + "\n", encoding="utf-8")
        out["checks"].append({"check": "pd10_run_state_aligned", "ok": True, "path": str(run_state_path)})

    orch = ControlledMonthlyImportOrchestrator(
        CATALOG_ROOT, progress_dir=PROGRESS / "august" / "15m_PD10"
    )
    dry = orch.dry_run(manifest, manifest_path=MANIFEST_PD10)
    out["checks"].append(
        {
            "check": "aug_pd10_orchestrator_dry_run",
            "ok": dry.status == "dry_run_ok",
            "status": dry.status,
            "errors": dry.errors,
        }
    )
    out["preflight"] = read_ch_import_preflight(pipeline_line_id=manifest.pipeline_line_id)
    out["overnight_resume_state"] = {
        "next_month_index": 2,
        "status": "resume",
        "last_completed_month": "2026-07",
        "august_monthly_resume": json.loads(
            (PROGRESS / "august" / "monthly_resume_state.json").read_text(encoding="utf-8")
        ),
    }
    out["ok"] = dry.status == "dry_run_ok"
    (LOG / "phase4z_resume_reconcile.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"ok": out["ok"], "report": str(LOG / "phase4z_resume_reconcile.json")}, indent=2))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

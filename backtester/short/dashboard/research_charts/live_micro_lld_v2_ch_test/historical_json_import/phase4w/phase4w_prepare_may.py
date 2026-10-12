#!/usr/bin/env python3
"""Phase 4W: May inventory, manifests, dry-run sequence (read-only CH)."""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
for _p in (_REPO, _DASH):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_runner import (
    estimate_batch_write_rows,
    validate_batch,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.catalog_inventory import (
    inventory_from_coverage,
    iter_stems,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.ch_import_resource_monitor import (
    check_optional_limits,
    collect_resource_baseline,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.ch_import_state import (
    ch_client_and_db,
    fetch_published_snapshot_ids,
    read_ch_import_preflight,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_monthly_sequence import (
    ControlledMonthlySequenceRunner,
    load_monthly_sequence_plan,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_run_manifest import (
    MAY_2026_COMBO_RUN_IDS,
    StemSlice,
    build_phase4w_may2026_combo_manifest,
    compute_scope_fingerprint,
    iter_manifest_batch_specs,
    resolve_manifest_stems,
    write_manifest_json,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.historical_batch_config import (
    PILOT_PIPELINE_LINE_ID,
    STRATEGY_COMBOS,
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
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.resource_limit_proposal import (
    proposed_500_batch_limit_environ,
)

MANIFEST_DIR = Path(__file__).resolve().parents[1] / "manifests"
LOG_DIR = _REPO / "logs" / "phase4w"
COMBO_ORDER = ("15m_PD1", "15m_PD5", "15m_PD10", "4h_PD10")
PLACEHOLDER_BACKUP = "00000000-0000-0000-0000-000000000001"


def _combo_inventory(catalog_root: Path, combo_folder: str) -> dict:
    month = "2026-05"
    stems = tuple(iter_stems(catalog_root, month=month, combo_folder=combo_folder))
    combo = next(c for c in STRATEGY_COMBOS if c.folder == combo_folder)
    line = combo.pipeline_line_id()
    manifest_plan = build_phase4w_may2026_combo_manifest(
        catalog_root, combo_folder, backup_recovery_id=PLACEHOLDER_BACKUP
    )
    specs = iter_manifest_batch_specs(catalog_root, manifest_plan, stems)
    vr_all = []
    snapshot_ids: list[str] = []
    for spec in specs:
        vr = validate_batch(catalog_root, spec)
        vr_all.append(vr)
        if vr.get("errors"):
            raise RuntimeError(f"validate failed {combo_folder}: {vr['errors']}")
        snapshot_ids.extend(vr.get("snapshot_ids") or [])

    expected = estimate_batch_write_rows(
        {
            "meta_rows": sum(int(v["summary"].get("meta_rows") or 0) for v in vr_all),
            "member_rows": sum(int(v["summary"].get("member_rows") or 0) for v in vr_all),
            "catalog_rows": sum(int(v["summary"].get("catalog_rows") or 0) for v in vr_all),
            "publish_rows": sum(int(v["summary"].get("publish_rows") or 0) for v in vr_all),
        }
    )
    expected["sidecar_rows"] = len(snapshot_ids)

    client, db = ch_client_and_db()
    try:
        published_line = fetch_published_snapshot_ids(client, db, pipeline_line_id=line)
        pilot_pub = fetch_published_snapshot_ids(client, db, pipeline_line_id=PILOT_PIPELINE_LINE_ID)
        layer_map = load_scope_layer_map_for_snapshots(snapshot_ids)
        pilot_ids = pilot_snapshot_ids_from_catalog(catalog_root)
        scope_checks = assess_scope_ch_states(
            snapshot_ids,
            pipeline_line_id=line,
            published_on_line=published_line,
            layer_by_snapshot=layer_map,
            pilot_snapshot_ids=pilot_ids,
        )
        assert_scope_recovery_safe(scope_checks)
        actions = Counter(c["recovery_action"] for c in scope_checks)
        overlap_pilot = sorted(set(snapshot_ids) & pilot_pub)
    finally:
        client.close()

    cov = [
        s
        for s in inventory_from_coverage(catalog_root)["slices"]
        if s["month"] == month and s["combo_folder"] == combo_folder
    ][0]

    stem_slice = StemSlice(0, len(stems))
    return {
        "combo_folder": combo_folder,
        "month": month,
        "pipeline_line_id": line,
        "run_id": MAY_2026_COMBO_RUN_IDS[combo_folder],
        "stem_count": len(stems),
        "first_stem": stems[0] if stems else "",
        "last_stem": stems[-1] if stems else "",
        "coverage_matrix": cov,
        "batch_count": len(specs),
        "last_batch_stems": len(specs[-1].stems) if specs else 0,
        "expected_writes": expected,
        "published_on_line_count": len(published_line),
        "pilot_line_overlap_snapshot_ids": overlap_pilot,
        "scope_recovery_actions": dict(actions),
        "scope_fingerprint": compute_scope_fingerprint(
            approved_month=month,
            combo_folder=combo_folder,
            pipeline_line_id=line,
            stem_slice=stem_slice,
            stems=stems,
        ),
    }


def main() -> int:
    backup_result_path = LOG_DIR / "phase4w_backup_restore_backup_result.json"
    if not backup_result_path.is_file():
        print(f"missing backup result: {backup_result_path}", file=sys.stderr)
        return 2
    backup = json.loads(backup_result_path.read_text(encoding="utf-8"))
    backup_id = str(backup["backup_id"])

    inventory = {"combos": [], "backup_recovery_id": backup_id}
    manifest_paths: dict[str, Path] = {}
    for combo in COMBO_ORDER:
        inv = _combo_inventory(CATALOG_ROOT, combo)
        inventory["combos"].append(inv)
        fname = f"phase4w_may2026_{combo.lower()}_full_v1.json"
        path = MANIFEST_DIR / fname
        manifest = build_phase4w_may2026_combo_manifest(
            CATALOG_ROOT, combo, backup_recovery_id=backup_id
        )
        if manifest.scope_fingerprint != inv["scope_fingerprint"]:
            raise RuntimeError(f"fingerprint drift {combo}")
        write_manifest_json(path, manifest)
        resolve_manifest_stems(CATALOG_ROOT, manifest)
        manifest_paths[combo] = path

    plan_path = MANIFEST_DIR / "phase4w_may2026_monthly_run_plan_v1.json"
    plan = {
        "schema_version": 1,
        "approved_month": "2026-05",
        "backup_recovery_id": backup_id,
        "combo_order": list(COMBO_ORDER),
        "manifests": [
            {"combo_folder": c, "manifest_path": str(manifest_paths[c].resolve())}
            for c in COMBO_ORDER
        ],
        "resource_limits": proposed_500_batch_limit_environ(),
        "write_gates_required": [
            "HISTORICAL_JSON_IMPORT_ALLOW_WRITE=1",
            "HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE=1",
            f"HISTORICAL_IMPORT_VERIFIED_BACKUP_RECOVERY_ID={backup_id}",
        ],
    }
    plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")

    binding = {
        "phase": "4W",
        "backup_tag": backup.get("backup_tag"),
        "operational_backup_recovery_id": backup_id,
        "monthly_plan": plan_path.name,
        "manifests": {c: manifest_paths[c].name for c in COMBO_ORDER},
    }
    (MANIFEST_DIR / "phase4w_may2026_backup_binding.json").write_text(
        json.dumps(binding, indent=2) + "\n", encoding="utf-8"
    )

    progress = CATALOG_ROOT / "_controlled_import_progress_phase4w_dry"
    seq_runner = ControlledMonthlySequenceRunner(CATALOG_ROOT, progress_dir=progress)
    seq_res = seq_runner.run(load_monthly_sequence_plan(plan_path), mode="dry_run")

    for key, val in proposed_500_batch_limit_environ().items():
        os.environ[key] = val
    baseline = collect_resource_baseline(
        CATALOG_ROOT,
        pipeline_line_id=STRATEGY_COMBOS[0].pipeline_line_id(),
        scope_snapshot_ids=set(),
    )

    april_lines = {}
    client, db = ch_client_and_db()
    try:
        for combo in STRATEGY_COMBOS:
            line = combo.pipeline_line_id()
            april_lines[combo.folder] = len(fetch_published_snapshot_ids(client, db, pipeline_line_id=line))
        april_lines["aug_pilot"] = len(
            fetch_published_snapshot_ids(client, db, pipeline_line_id=PILOT_PIPELINE_LINE_ID)
        )
    finally:
        client.close()

    out = {
        "inventory": inventory,
        "monthly_dry_run": {
            "status": seq_res.status,
            "steps": seq_res.steps,
            "errors": seq_res.errors,
        },
        "resource_health": {
            "baseline": baseline,
            "limits_with_proposed_env": check_optional_limits(baseline),
            "preflight": read_ch_import_preflight(
                pipeline_line_id=STRATEGY_COMBOS[0].pipeline_line_id()
            ),
        },
        "april_publish_lines_unchanged": april_lines,
    }
    out_path = LOG_DIR / "phase4w_may_prepare_result.json"
    out_path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"ok": seq_res.status == "dry_run_ok", "out": str(out_path)}, indent=2))
    return 0 if seq_res.status == "dry_run_ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

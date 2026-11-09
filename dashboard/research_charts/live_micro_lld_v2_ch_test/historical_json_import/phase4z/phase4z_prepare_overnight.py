#!/usr/bin/env python3
"""Phase 4Z: inventory Jun–Sep, manifests, overnight plan, full dry-run."""

from __future__ import annotations

import json
import os
import sys
import uuid
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
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_overnight_sequence import (
    ControlledOvernightSequenceRunner,
    load_overnight_import_plan,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.controlled_run_manifest import (
    PHASE4Z_MONTH_SLUG,
    StemSlice,
    build_phase4z_month_combo_manifest,
    compute_scope_fingerprint,
    iter_manifest_batch_specs,
    load_controlled_run_manifest,
    phase4z_combo_run_id,
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
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.pilot_overlap import (
    pilot_snapshot_ids_from_catalog,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.productive_prewrite_gate import (
    assess_scope_ch_states,
    assert_scope_recovery_safe,
    load_scope_layer_map_for_snapshots,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.resource_limit_proposal import (
    proposed_500_batch_limit_environ,
)

MANIFEST_DIR = Path(__file__).resolve().parents[1] / "manifests"
LOG_DIR = _REPO / "logs" / "historical_import" / "overnight_2026"
COMBO_ORDER = ("15m_PD1", "15m_PD5", "15m_PD10", "4h_PD10")
APPROVED_MONTHS = ("2026-06", "2026-07", "2026-08", "2026-09")
PROGRESS_SUBDIRS = {
    "2026-06": "june",
    "2026-07": "july",
    "2026-08": "august",
    "2026-09": "september",
}
OVERNIGHT_PLAN_NAME = "june_sep_2026_overnight_import_plan_v1.json"


def _combo_inventory(catalog_root: Path, month: str, combo_folder: str) -> dict:
    stems = tuple(iter_stems(catalog_root, month=month, combo_folder=combo_folder))
    if not stems:
        raise RuntimeError(f"no stems for {month} {combo_folder}")
    seen = set()
    dupes = []
    for s in stems:
        if s in seen:
            dupes.append(s)
        seen.add(s)
    combo = next(c for c in STRATEGY_COMBOS if c.folder == combo_folder)
    line = combo.pipeline_line_id()
    manifest_plan = build_phase4z_month_combo_manifest(
        catalog_root, month, combo_folder, backup_recovery_id="placeholder"
    )
    specs = iter_manifest_batch_specs(catalog_root, manifest_plan, stems)
    vr_all = []
    snapshot_ids: list[str] = []
    for spec in specs:
        vr = validate_batch(catalog_root, spec)
        vr_all.append(vr)
        if vr.get("errors"):
            raise RuntimeError(f"validate failed {month} {combo_folder}: {vr['errors']}")
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
        overlap_pilot_publish = sorted(set(snapshot_ids) & pilot_pub)
        overlap_pilot_catalog = sorted(set(snapshot_ids) & pilot_ids)
    finally:
        client.close()

    cov = [
        s
        for s in inventory_from_coverage(catalog_root)["slices"]
        if s["month"] == month and s["combo_folder"] == combo_folder
    ]
    if len(cov) != 1:
        raise RuntimeError(f"coverage slice missing for {month} {combo_folder}")

    stem_slice = StemSlice(0, len(stems))
    fp = compute_scope_fingerprint(
        approved_month=month,
        combo_folder=combo_folder,
        pipeline_line_id=line,
        stem_slice=stem_slice,
        stems=stems,
    )
    return {
        "combo_folder": combo_folder,
        "month": month,
        "pipeline_line_id": line,
        "run_id": phase4z_combo_run_id(month, combo_folder),
        "stem_count": len(stems),
        "stems_sorted_sha256": fp,
        "first_stem": stems[0],
        "last_stem": stems[-1],
        "duplicate_stems": dupes,
        "coverage_matrix": cov[0],
        "batch_count": len(specs),
        "last_batch_stems": len(specs[-1].stems) if specs else 0,
        "expected_writes": expected,
        "published_on_line_count": len(published_line),
        "pilot_line_overlap_snapshot_ids": overlap_pilot_publish,
        "pilot_catalog_overlap_snapshot_ids": overlap_pilot_catalog,
        "scope_recovery_actions": dict(actions),
        "scope_fingerprint": fp,
        "sorted_stems": list(stems),
    }


def _verify_june_phase4y_fingerprints(inv_combo: dict, combo: str) -> None:
    slug = PHASE4Z_MONTH_SLUG["2026-06"]
    legacy = MANIFEST_DIR / f"phase4y_{slug}_{combo.lower()}_full_v1.json"
    if not legacy.is_file():
        return
    old = load_controlled_run_manifest(legacy)
    if old.scope_fingerprint != inv_combo["scope_fingerprint"]:
        raise RuntimeError(
            f"June {combo} fingerprint drift vs phase4y: "
            f"{old.scope_fingerprint} != {inv_combo['scope_fingerprint']}"
        )


def main() -> int:
    backup_result_path = LOG_DIR / "phase4z_pre_june_sep_backup_backup_result.json"
    if not backup_result_path.is_file():
        print(f"missing backup result: {backup_result_path}", file=sys.stderr)
        return 2
    backup = json.loads(backup_result_path.read_text(encoding="utf-8"))
    backup_id = str(backup["backup_id"])

    pilot_ids = sorted(pilot_snapshot_ids_from_catalog(CATALOG_ROOT))
    august_pilot_audit = {
        "pilot_snapshot_count": len(pilot_ids),
        "pilot_snapshot_ids": pilot_ids,
    }

    inventory: dict = {
        "backup_recovery_id": backup_id,
        "months": {},
        "totals": {
            "snapshots": 0,
            "batches": 0,
            "meta_rows": 0,
            "member_rows": 0,
            "catalog_rows": 0,
            "publish_rows": 0,
            "sidecar_rows": 0,
        },
    }
    monthly_plan_paths: dict[str, Path] = {}
    manifest_registry: list[dict] = []

    for month in APPROVED_MONTHS:
        slug = PHASE4Z_MONTH_SLUG[month]
        month_inv = {"combos": []}
        manifest_paths: dict[str, Path] = {}
        for combo in COMBO_ORDER:
            inv = _combo_inventory(CATALOG_ROOT, month, combo)
            if month == "2026-06":
                _verify_june_phase4y_fingerprints(inv, combo)
            if month == "2026-08" and combo == "15m_PD10":
                august_pilot_audit["august_pd10_pilot_catalog_overlap"] = inv[
                    "pilot_catalog_overlap_snapshot_ids"
                ]
                august_pilot_audit["august_pd10_pilot_publish_overlap"] = inv[
                    "pilot_line_overlap_snapshot_ids"
                ]
                august_pilot_audit["august_pd10_scope_actions"] = inv["scope_recovery_actions"]
            month_inv["combos"].append({k: v for k, v in inv.items() if k != "sorted_stems"})
            fname = f"phase4z_{slug}_{combo.lower()}_full_v1.json"
            path = MANIFEST_DIR / fname
            manifest = build_phase4z_month_combo_manifest(
                CATALOG_ROOT, month, combo, backup_recovery_id=backup_id
            )
            if manifest.scope_fingerprint != inv["scope_fingerprint"]:
                raise RuntimeError(f"fingerprint drift {month} {combo}")
            write_manifest_json(path, manifest)
            resolve_manifest_stems(CATALOG_ROOT, manifest)
            manifest_paths[combo] = path
            manifest_registry.append(
                {
                    "month": month,
                    "combo_folder": combo,
                    "manifest_path": str(path.resolve()),
                    "scope_fingerprint": manifest.scope_fingerprint,
                    "run_id": manifest.run_id,
                    "max_snapshots": manifest.max_snapshots,
                    "max_batches": manifest.max_batches,
                }
            )
            inventory["totals"]["snapshots"] += inv["stem_count"]
            inventory["totals"]["batches"] += inv["batch_count"]
            ew = inv["expected_writes"]
            inventory["totals"]["meta_rows"] += int(ew.get("meta_rows") or 0)
            inventory["totals"]["member_rows"] += int(ew.get("member_rows") or 0)
            inventory["totals"]["catalog_rows"] += int(ew.get("catalog_rows") or 0)
            inventory["totals"]["publish_rows"] += int(ew.get("publish_rows") or 0)
            inventory["totals"]["sidecar_rows"] += int(ew.get("sidecar_rows") or 0)

        plan_path = MANIFEST_DIR / f"phase4z_{slug}_monthly_run_plan_v1.json"
        plan = {
            "schema_version": 1,
            "approved_month": month,
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
        monthly_plan_paths[month] = plan_path
        inventory["months"][month] = month_inv

    overnight_run_id = str(uuid.uuid4())
    overnight_plan_path = MANIFEST_DIR / OVERNIGHT_PLAN_NAME
    overnight_plan = {
        "schema_version": 1,
        "overnight_run_id": overnight_run_id,
        "backup_recovery_id": backup_id,
        "symbol": "DOGEUSDT",
        "approved_months": list(APPROVED_MONTHS),
        "combo_order": list(COMBO_ORDER),
        "manifest_registry": manifest_registry,
        "months": [
            {
                "month": m,
                "progress_subdir": PROGRESS_SUBDIRS[m],
                "monthly_plan_path": str(monthly_plan_paths[m].resolve()),
            }
            for m in APPROVED_MONTHS
        ],
    }
    overnight_plan_path.write_text(json.dumps(overnight_plan, indent=2) + "\n", encoding="utf-8")

    progress_dry = CATALOG_ROOT / "_controlled_import_progress_phase4z_dry"
    overnight_runner = ControlledOvernightSequenceRunner(CATALOG_ROOT, progress_root=progress_dry)
    overnight_res = overnight_runner.run(
        load_overnight_import_plan(overnight_plan_path),
        mode="dry_run",
        state_path=progress_dry / "dry_run_state.json",
        log_root=LOG_DIR,
    )

    for key, val in proposed_500_batch_limit_environ().items():
        os.environ[key] = val
    baseline = collect_resource_baseline(
        CATALOG_ROOT,
        pipeline_line_id=STRATEGY_COMBOS[0].pipeline_line_id(),
        scope_snapshot_ids=set(),
    )

    out = {
        "inventory": inventory,
        "august_pilot_audit": august_pilot_audit,
        "overnight_plan_path": str(overnight_plan_path),
        "overnight_run_id": overnight_run_id,
        "overnight_dry_run": {
            "status": overnight_res.status,
            "months": overnight_res.months,
            "errors": overnight_res.errors,
        },
        "resource_health": {
            "baseline": baseline,
            "limits_with_proposed_env": check_optional_limits(baseline),
            "preflight": read_ch_import_preflight(
                pipeline_line_id=STRATEGY_COMBOS[0].pipeline_line_id()
            ),
        },
    }
    out_path = LOG_DIR / "phase4z_preflight_all_months.json"
    out_path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": overnight_res.status == "dry_run_ok",
                "backup_id": backup_id,
                "overnight_run_id": overnight_run_id,
                "out": str(out_path),
            },
            indent=2,
        )
    )
    return 0 if overnight_res.status == "dry_run_ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

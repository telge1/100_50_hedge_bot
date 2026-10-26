#!/usr/bin/env python3
"""Phase 4X-AU prestart (read-only)."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AO = ROOT / "tools/phase4xao_jun_sep_nohup_rollout_v1"
OUT = ROOT / "results/phase4xau_final_september_resume_v1"
RESULTS_AT = ROOT / "results/phase4xat_september_mixed_batch_recovery_v1"

PIPELINE = "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1"
PILOT_LOOKUP = "provenance_lookup_v1:185ddfc1ca31f9bc"
NEW_SNAPSHOT = "6c2e01d76a1559a94ec373abdb4b96bc73f3ddaba89bd1ee1058b001d04d6d61"
BATCH46_PARTIAL = (
    "20260908T234000Z",
    "20260908T234500Z",
    "20260908T235000Z",
    "20260908T235500Z",
)
SEP_PILOT_START = "20260909T000000Z"
SEP_PILOT_END = "20260917T235500Z"


def _setup() -> None:
    for p in (ROOT, AO, ROOT / "backtester/short/dashboard", Path("/home/telgenbuescher/projects/trading_research_platform")):
        if p.is_dir() and str(p) not in sys.path:
            sys.path.insert(0, str(p))
    from research_charts.trp_import import ensure_trp_on_path

    ensure_trp_on_path()


def pgrep_runners() -> list[str]:
    r = subprocess.run(
        ["pgrep", "-af", "run_jun_sep_nohup.py"],
        capture_output=True,
        text=True,
    )
    lines = [ln for ln in (r.stdout or "").splitlines() if "python" in ln and "run_prestart" not in ln]
    return lines


def run_subprocess(cmd: list[str], cwd: Path) -> dict:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return {"cmd": cmd, "returncode": p.returncode, "stdout_tail": p.stdout[-2000:], "stderr_tail": p.stderr[-2000:]}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    report: dict = {"utc": datetime.now(timezone.utc).isoformat(), "checks": {}, "blockers": []}

    runners = pgrep_runners()
    report["checks"]["no_active_runner"] = {"ok": len(runners) == 0, "lines": runners}
    if runners:
        report["blockers"].append("active_runner")

    pid_file = ROOT / "results/phase4xao_jun_sep_nohup_rollout_v1/pid"
    stale_pid = None
    if pid_file.is_file():
        stale_pid = pid_file.read_text().strip()
        alive = subprocess.run(["kill", "-0", stale_pid], capture_output=True).returncode == 0
        report["checks"]["pid_file"] = {"path": str(pid_file), "pid": stale_pid, "alive": alive}
        if alive:
            report["blockers"].append(f"pid_file_alive:{stale_pid}")
    else:
        report["checks"]["pid_file"] = {"ok": True, "missing": True}

    state_path = ROOT / "results/phase4xao_jun_sep_nohup_rollout_v1/progress.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    report["checks"]["progress_json"] = state
    ms_sep = state["months"]["2026-09"]
    for m in ("2026-06", "2026-07", "2026-08"):
        mm = state["months"][m]
        ok = mm.get("validation") == "MONTH_VALIDATION_PASS" and mm.get("publish") == "MONTH_PUBLISH_PASS"
        report["checks"][f"jun_aug_{m}"] = ok
        if not ok:
            report["blockers"].append(f"{m}_not_pass")
    if ms_sep.get("validation") != "MONTH_VALIDATION_PASS":
        report["blockers"].append("sep_validation_not_pass")
    if int(ms_sep.get("verified_close_count", -1)) != 2300:
        report["blockers"].append(f"verified_close_count={ms_sep.get('verified_close_count')}")
    if int(ms_sep.get("last_verified_batch_index", -1)) != 45:
        report["blockers"].append(f"last_verified_batch_index={ms_sep.get('last_verified_batch_index')}")

    tests = run_subprocess([sys.executable, str(ROOT / "tools/phase4xat_september_mixed_batch_recovery_v1/test_mixed_batch.py")], ROOT)
    report["checks"]["tests_4xat"] = tests
    if tests["returncode"] != 0:
        report["blockers"].append("4xat_tests_failed")

    dry = run_subprocess([sys.executable, str(ROOT / "tools/phase4xat_september_mixed_batch_recovery_v1/run_dry_run.py")], ROOT)
    report["checks"]["dry_run_4xat"] = dry
    if dry["returncode"] != 0:
        report["blockers"].append("4xat_dry_run_failed")
    if (RESULTS_AT / "dry_run_batch46.json").is_file():
        dr = json.loads((RESULTS_AT / "dry_run_batch46.json").read_text())
        report["checks"]["batch46_dry_run"] = dr
        if not dr.get("readback_gate_ok"):
            report["blockers"].append("batch46_readback_fail")

    _setup()
    from import_path_5m_pd1 import setup_hcfg
    from run_jun_sep_nohup import build_month_plan, ch_ledger, published_stems_month
    from mixed_batch_import_v1 import assert_published_stems_integrity
    from september_immutable_binding_v1 import binding_env_enabled, resolve_immutable_input

    hcfg = setup_hcfg()
    ledger = ch_ledger(hcfg)
    report["checks"]["ch_ledger"] = ledger
    sep_month = ledger["by_month"].get("2026-09", 0)
    if sep_month != 4896:
        report["blockers"].append(f"sep_ledger_{sep_month}_!=_4896")
    if ledger["sep_window"] != 2592:
        report["blockers"].append(f"sep_window_{ledger['sep_window']}")

    pub = published_stems_month(hcfg, "2026-09")
    for s in BATCH46_PARTIAL:
        if s not in pub:
            report["blockers"].append(f"missing_partial_{s}")
    report["checks"]["batch46_partial_published"] = {s: s in pub for s in BATCH46_PARTIAL}

    plan = build_month_plan("2026-09")
    stems46 = [c["stem"] for c in plan["batches"][46]["closes"]]
    try:
        assert_published_stems_integrity(
            catalog_root=ROOT
            / "backtester/short/dashboard/research_charts/live_micro_lld_v2_ch_test/doge_apr_sep_2026_v1_history",
            month="2026-09",
            combo_folder="5m_PD1",
            pipeline_line_id=PIPELINE,
            config_hash=hcfg.config_hash,
            published_stems=pub,
            batch_stems=stems46,
        )
        report["checks"]["batch46_integrity"] = True
    except Exception as exc:
        report["checks"]["batch46_integrity"] = str(exc)
        report["blockers"].append("batch46_integrity")

    import clickhouse_connect
    from research_charts.clickhouse_config import load_clickhouse_config

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database
    try:
        pilot_pub = client.query(
            f"""
            SELECT count() FROM {db}.research_lmv2_active_snapshot_publish_v1 FINAL
            WHERE pipeline_line_id = {{lid:String}} AND config_hash = {{ch:String}}
              AND publish_complete = 1
              AND decision_close >= '2026-09-09' AND decision_close < '2026-09-18'
            """,
            parameters={"lid": hcfg.pipeline_line_id, "ch": hcfg.config_hash},
        ).result_rows[0][0]
        pilot_side = client.query(
            f"""
            SELECT count(), min(provenance_lookup_version), max(provenance_lookup_version)
            FROM {db}.research_lmv2_snapshot_import_provenance_v1
            WHERE pipeline_line_id = {{lid:String}}
              AND relative_json_path LIKE '%/2026-09/5m_PD1/%'
              AND provenance_lookup_version = {{lk:String}}
            """,
            parameters={"lid": PIPELINE, "lk": PILOT_LOOKUP},
        ).result_rows[0]
        pilot_fingerprints = client.query(
            f"""
            SELECT cityHash64(groupArray(tuple(
              snapshot_id, dest_sha256, frozen_price_hash, provenance_lookup_version
            )))
            FROM {db}.research_lmv2_snapshot_import_provenance_v1
            WHERE pipeline_line_id = {{lid:String}} AND provenance_lookup_version = {{lk:String}}
              AND relative_json_path LIKE '%/2026-09/5m_PD1/%'
            """,
            parameters={"lid": PIPELINE, "lk": PILOT_LOOKUP},
        ).result_rows[0][0]
    finally:
        client.close()

    report["checks"]["pilot"] = {
        "publish_count_window": int(pilot_pub),
        "sidecar_pilot_lookup_count": int(pilot_side[0]),
        "lookup_min_max": (pilot_side[1], pilot_side[2]),
        "sidecar_aggregate_fingerprint": int(pilot_fingerprints),
    }
    if int(pilot_pub) != 2592 or int(pilot_side[0]) != 2592:
        report["blockers"].append("pilot_count_mismatch")
    if pilot_side[1] != PILOT_LOOKUP or pilot_side[2] != PILOT_LOOKUP:
        report["blockers"].append("pilot_lookup_not_uniform")

    os_env_binding = True
    import os

    os.environ["JUN_SEP_SEPTEMBER_IMMUTABLE_BINDING_V1"] = "1"
    pin = resolve_immutable_input("2026-09")
    report["checks"]["snapshot_pin"] = {
        "binding_enabled": binding_env_enabled(),
        "snapshot_id": pin.snapshot_id,
        "mode": pin.binding_mode,
    }
    if pin.snapshot_id != NEW_SNAPSHOT:
        report["blockers"].append("snapshot_pin_mismatch")

    pf = run_subprocess(
        [
            sys.executable,
            str(AO / "run_jun_sep_nohup.py"),
            "--preflight",
        ],
        ROOT,
    )
    report["checks"]["runner_preflight"] = pf
    if pf["returncode"] != 0:
        report["blockers"].append("runner_preflight_failed")

    report["ok"] = len(report["blockers"]) == 0
    report["verdict"] = "PRESTART_PASS" if report["ok"] else "PRESTART_BLOCKED"
    out_path = OUT / "prestart_result.json"
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "verdict": report["verdict"], "blockers": report["blockers"]}, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

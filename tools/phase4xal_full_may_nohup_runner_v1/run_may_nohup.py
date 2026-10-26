#!/usr/bin/env python3
"""
Phase 4X-AL — Full May 5m PD1 density nohup runner.

CH writes only with: --execute AND MAY_NOHUP_EXECUTE_APPROVED=1
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results/phase4xal_full_may_nohup_runner_v1"
REPORT = ROOT / "reports/DOGEUSDT_PHASE4XAL_FULL_MAY_NOHUP_RUNNER_V1.md"
BATCH_PLAN_PATH = ROOT / "results/phase4xaj_full_may_publish_readiness_v1/full_may_batch_plan.json"
PARITY_CSV = ROOT / "results/phase4xab_full_may_density_continuity_v1/active_origin_parity_by_close.csv"

SPREAD_DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
POOLS_DASH = ROOT / "backtester/short/dashboard"
TRP = Path("/home/telgenbuescher/projects/trading_research_platform")
APR_SEP = POOLS_DASH / "research_charts/live_micro_lld_v2_ch_test/doge_apr_sep_2026_v1_history"
MULTI_MAY = (
    POOLS_DASH
    / "research_charts/live_micro_lld_v2_ch_test/doge_multi_month_2026_history"
    / "production/results/2026-05/5m_PD1"
)

PIPELINE_LINE = "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1"
MONTH = "2026-05"
COMBO = "5m_PD1"
EXPECTED_MAY = 8928
SEP_WINDOW_START = "2026-09-09"
SEP_WINDOW_END = "2026-09-18"
SEP_EXPECTED = 2592

STATE_PATH = OUT / "progress.json"
PID_PATH = OUT / "may_nohup.pid"
EXIT_PATH = OUT / "exit_code.txt"
VERDICT_PATH = OUT / "final_verdict.md"
APPROVAL_ENV = "MAY_NOHUP_EXECUTE_APPROVED"
LOCK_SCRIPT_NAME = "run_may_nohup.py"


def _setup() -> Any:
    dash = SPREAD_DASH if SPREAD_DASH.is_dir() else POOLS_DASH
    for mod in list(sys.modules):
        if mod.startswith("research_charts"):
            del sys.modules[mod]
    sys.path = [p for p in sys.path if p != str(POOLS_DASH)]
    for p in (ROOT, TRP, dash):
        if p.is_dir() and str(p) not in sys.path:
            sys.path.insert(0, str(p))
    from research_charts.trp_import import ensure_trp_on_path

    ensure_trp_on_path()
    from research_charts.historical_lmv2_pd_contract_v1 import resolve_viewer_historical_scope

    return resolve_viewer_historical_scope(timeframe="5m", pool_detail=1)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "runner.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_batch_plan() -> dict[str, Any]:
    if not BATCH_PLAN_PATH.is_file():
        raise SystemExit(f"missing batch plan {BATCH_PLAN_PATH}")
    return json.loads(BATCH_PLAN_PATH.read_text(encoding="utf-8"))


def load_state() -> dict[str, Any]:
    if STATE_PATH.is_file():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {
        "last_completed_batch_index": -1,
        "last_verified_batch_index": -1,
        "pending_readback_batch_index": None,
        "verified_close_count": 0,
        "published_may_at_start": None,
        "final_verdict": None,
        "updated_at_utc": None,
    }


def save_state(state: dict[str, Any]) -> None:
    state["updated_at_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    OUT.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def ch_ledger(hcfg: Any) -> dict[str, int]:
    import clickhouse_connect
    from research_charts.clickhouse_config import load_clickhouse_config
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.publish_schema import TABLE_PUBLISH

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database
    try:
        may = client.query(
            f"""
            SELECT count() FROM {db}.{TABLE_PUBLISH} FINAL
            WHERE pipeline_line_id = {{lid:String}} AND config_hash = {{ch:String}}
              AND publish_complete = 1
              AND decision_close >= '2026-05-01' AND decision_close < '2026-06-01'
            """,
            parameters={"lid": hcfg.pipeline_line_id, "ch": hcfg.config_hash},
        ).result_rows[0][0]
        sep = client.query(
            f"""
            SELECT count() FROM {db}.{TABLE_PUBLISH} FINAL
            WHERE pipeline_line_id = {{lid:String}} AND config_hash = {{ch:String}}
              AND publish_complete = 1
              AND decision_close >= {{s:String}} AND decision_close < {{e:String}}
            """,
            parameters={
                "lid": hcfg.pipeline_line_id,
                "ch": hcfg.config_hash,
                "s": SEP_WINDOW_START,
                "e": SEP_WINDOW_END,
            },
        ).result_rows[0][0]
        return {"may_published": int(may), "sep_window": int(sep)}
    finally:
        client.close()


def published_stems_set(hcfg: Any) -> set[str]:
    import clickhouse_connect
    from research_charts.clickhouse_config import load_clickhouse_config
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.publish_schema import TABLE_PUBLISH

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database
    try:
        rows = client.query(
            f"""
            SELECT decision_close FROM {db}.{TABLE_PUBLISH} FINAL
            WHERE pipeline_line_id = {{lid:String}} AND config_hash = {{ch:String}}
              AND publish_complete = 1
              AND decision_close >= '2026-05-01' AND decision_close < '2026-06-01'
            """,
            parameters={"lid": hcfg.pipeline_line_id, "ch": hcfg.config_hash},
        ).result_rows
        out = set()
        for (dc,) in rows:
            if dc.tzinfo is None:
                dc = dc.replace(tzinfo=timezone.utc)
            out.add(dc.strftime("%Y%m%dT%H%M%SZ"))
        return out
    finally:
        client.close()


def _running_import_lock(check_execute_runner: bool = False) -> list[str]:
    out = []
    r2 = subprocess.run(["pgrep", "-af", "run_completion_import"], capture_output=True, text=True)
    for line in (r2.stdout or "").strip().splitlines():
        out.append(line)
    if check_execute_runner and PID_PATH.is_file():
        pid = PID_PATH.read_text().strip()
        if pid and pid != str(os.getpid()) and subprocess.run(["kill", "-0", pid], capture_output=True).returncode == 0:
            out.append(f"pid_file_active:{pid}")
    return out


def acquire_pid_file() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    if PID_PATH.is_file():
        old = PID_PATH.read_text().strip()
        if old and subprocess.run(["kill", "-0", old], capture_output=True).returncode == 0:
            raise SystemExit(f"another runner alive pid={old}")
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")


def release_pid_file() -> None:
    if PID_PATH.is_file():
        PID_PATH.unlink(missing_ok=True)


def execute_approved() -> bool:
    return os.environ.get(APPROVAL_ENV, "").strip() == "1"


def preflight(hcfg: Any, plan: dict[str, Any]) -> dict[str, Any]:
    blockers: list[str] = []
    ledger = ch_ledger(hcfg)
    published = published_stems_set(hcfg)
    plan_stems = {c["stem"] for b in plan["batches"] for c in b["closes"]}
    unpublished_in_plan = len(plan_stems - published)
    if len(published) + unpublished_in_plan != EXPECTED_MAY:
        blockers.append(
            f"published {len(published)} + unpublished_in_plan {unpublished_in_plan} != {EXPECTED_MAY}"
        )
    remaining_plan = unpublished_in_plan
    if ledger["sep_window"] != SEP_EXPECTED:
        blockers.append(f"sep_window {ledger['sep_window']} != {SEP_EXPECTED}")
    if _running_import_lock(check_execute_runner=True):
        blockers.append("concurrent import process")
    du = shutil.disk_usage(APR_SEP)
    if du.free < 2 * 1024**3:
        blockers.append(f"disk < 2GiB free ({du.free})")
    if not PARITY_CSV.is_file():
        blockers.append("missing 4X-AB parity CSV")
    if plan.get("batch_size", 0) > 50:
        blockers.append("batch_size > 50")
    # spot validate_batch on batch 0 (read-only)
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_runner import (
        BatchSpec,
        validate_batch,
    )

    b0 = plan["batches"][0]
    stems0 = [c["stem"] for c in b0["closes"]]
    for stem in stems0[:3]:
        src = MULTI_MAY / f"{stem}.json"
        if not src.is_file():
            blockers.append(f"missing frozen {stem}")
    spec = BatchSpec(
        month=MONTH,
        combo_folder=COMBO,
        stems=tuple(stems0),
        pipeline_line_id=PIPELINE_LINE,
        import_batch_id=b0["import_batch_id"],
    )
    # validate needs files on apr_sep or we validate after mirror in execute — dry check frozen only
    val_errors = []
    for stem in stems0:
        rel = APR_SEP / "production/results" / MONTH / COMBO / f"{stem}.json"
        if not rel.is_file():
            val_errors.append(f"apr_sep missing {stem} (expected mirror at execute)")
    return {
        "ok": len(blockers) == 0,
        "blockers": blockers,
        "ledger": ledger,
        "published_stems": len(published),
        "remaining_in_plan": remaining_plan,
        "apr_sep_mirror_gaps_first_batch": val_errors[:5],
        "apr_sep_mirror_gap_count_first_batch": len(val_errors),
        "disk_free_gb": round(du.free / 1024**3, 2),
        "execute_approved": execute_approved(),
    }


def dry_run(hcfg: Any, plan: dict[str, Any]) -> dict[str, Any]:
    published = published_stems_set(hcfg)
    batches = []
    would_import = 0
    would_skip = 0
    for b in plan["batches"]:
        stems = [c["stem"] for c in b["closes"]]
        new = [s for s in stems if s not in published]
        skip = len(stems) - len(new)
        would_import += len(new)
        would_skip += skip
        batches.append(
            {
                "batch_index": b["batch_index"],
                "import_batch_id": b["import_batch_id"],
                "stems": len(stems),
                "would_import": len(new),
                "would_skip_published": skip,
            }
        )
    return {
        "batch_count": len(batches),
        "would_import_closes": would_import,
        "would_skip_idempotent": would_skip,
        "already_published": len(published),
        "target_may_total": EXPECTED_MAY,
        "batches": batches[:5],
        "batches_truncated": len(batches) > 5,
    }


def mirror_plan_remaining(plan: dict[str, Any], published: set[str]) -> dict[str, int]:
    """One-time mirror of all plan stems not yet on apr_sep (idempotent)."""
    all_stems = [c["stem"] for b in plan["batches"] for c in b["closes"]]
    todo = [s for s in all_stems if s not in published]
    if not todo:
        return {"mirrored": 0, "total_plan": len(all_stems)}
    backup = OUT / "apr_sep_backups" / "full_plan_mirror"
    chunk = 200
    for i in range(0, len(todo), chunk):
        mirror_batch_stems(todo[i : i + chunk], backup)
        _log(f"plan mirror chunk {i//chunk + 1} stems={min(i+chunk, len(todo))}/{len(todo)}")
    return {"mirrored": len(todo), "total_plan": len(all_stems)}


def mirror_batch_stems(stems: list[str], backup_dir: Path) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    lookup_path = APR_SEP / "production/provenance_lookup.json"
    if not (backup_dir / "provenance_lookup.json.bak").is_file():
        shutil.copy2(lookup_path, backup_dir / "provenance_lookup.json.bak")
    dest = APR_SEP / "production/results" / MONTH / COMBO
    dest.mkdir(parents=True, exist_ok=True)
    lookup = json.loads(lookup_path.read_text(encoding="utf-8"))
    for stem in stems:
        src = MULTI_MAY / f"{stem}.json"
        dst = dest / f"{stem}.json"
        if not dst.is_file():
            shutil.copy2(src, dst)
        elif _sha256_file(src) != _sha256_file(dst):
            raise RuntimeError(f"hash conflict {stem}")
        rel = f"production/results/{MONTH}/{COMBO}/{stem}.json"
        fid = str(json.loads(src.read_text(encoding="utf-8")).get("frozen_input", {}).get("snapshot_id") or "")
        lookup.setdefault("entries", {})[rel] = {"source_frozen_id": fid, "dest_sha256": _sha256_file(dst)}
    lookup["entry_count"] = len(lookup.get("entries") or {})
    lookup_path.write_text(json.dumps(lookup, indent=2, sort_keys=True), encoding="utf-8")
    from research_charts.historical_lmv2 import provenance_lookup as _pl

    _pl._LOOKUP_CACHE.clear()


def run_import_batch(stems: list[str], batch_id: str) -> tuple[list[dict], float]:
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_runner import BatchSpec
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_write import execute_batch_import

    os.environ["HISTORICAL_JSON_IMPORT_ALLOW_WRITE"] = "1"
    os.environ["HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE"] = "1"
    spec = BatchSpec(
        month=MONTH,
        combo_folder=COMBO,
        stems=tuple(stems),
        pipeline_line_id=PIPELINE_LINE,
        import_batch_id=batch_id,
    )
    t0 = time.perf_counter()
    outcomes = execute_batch_import(APR_SEP, spec, test_run_id=str(uuid.uuid4()))
    return outcomes, time.perf_counter() - t0


def optimized_readback(hcfg: Any, stems: list[str]) -> list[dict[str, Any]]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import clickhouse_connect
    from research_charts.clickhouse_config import load_clickhouse_config
    from research_charts.historical_pool_state_batch_reader_v1 import build_episode_walk_batch_cache
    from research_charts.historical_pool_state_v1 import clear_published_closes_cache
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (
        PublishedActiveReader,
    )

    from readback_gate import gate_ok, optimized_readback_batch, parity_index

    clear_published_closes_cache()
    density_ref = parity_index()
    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    database = cfg.database
    closes_dt = [datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc) for s in stems]
    cache = build_episode_walk_batch_cache(
        client, database, PIPELINE_LINE, hcfg.config_hash, closes_dt
    )
    reader = PublishedActiveReader()
    rows = optimized_readback_batch(hcfg, stems, density_ref, cache, client, database, reader)
    client.close()
    if not gate_ok(rows):
        raise RuntimeError(f"readback gate failed for batch stems {stems[0]}..{stems[-1]}")
    return rows


def write_batch_parity_csv(batch_index: int, rows: list[dict[str, Any]]) -> Path:
    p = OUT / "parity_by_batch" / f"batch_{batch_index:04d}.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return p


def cmd_status(hcfg: Any, plan: dict[str, Any]) -> int:
    state = load_state()
    ledger = ch_ledger(hcfg)
    published = len(published_stems_set(hcfg))
    remaining = EXPECTED_MAY - published
    print(json.dumps({"state": state, "ledger": ledger, "published_stems": published, "remaining": remaining}, indent=2))
    return 0


def cmd_preflight(hcfg: Any, plan: dict[str, Any]) -> int:
    result = preflight(hcfg, plan)
    (OUT / "preflight_result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    _log(f"preflight ok={result['ok']} blockers={result['blockers']}")
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def cmd_dry_run(hcfg: Any, plan: dict[str, Any]) -> int:
    result = dry_run(hcfg, plan)
    (OUT / "dry_run_result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    _log(f"dry_run would_import={result['would_import_closes']}")
    print(json.dumps(result, indent=2))
    return 0


def cmd_execute(hcfg: Any, plan: dict[str, Any], resume: bool) -> int:
    if not execute_approved():
        _log(f"REFUSED --execute: set {APPROVAL_ENV}=1 after explicit user approval")
        EXIT_PATH.write_text("2", encoding="utf-8")
        return 2
    if _running_import_lock(check_execute_runner=True):
        _log("REFUSED --execute: concurrent import or active pid file")
        return 2
    acquire_pid_file()
    state = load_state()
    exit_code = 0
    try:
        pf = preflight(hcfg, plan)
        if not pf["ok"]:
            raise RuntimeError(f"preflight blocked: {pf['blockers']}")
        if state["published_may_at_start"] is None:
            state["published_may_at_start"] = pf["ledger"]["may_published"]
            save_state(state)

        start_batch = 0 if not resume else max(0, state["last_verified_batch_index"] + 1)
        if state.get("pending_readback_batch_index") is not None:
            start_batch = int(state["pending_readback_batch_index"])

        sep_before = ch_ledger(hcfg)["sep_window"]
        published = published_stems_set(hcfg)
        if state.get("full_plan_mirrored") != True:
            meta = mirror_plan_remaining(plan, published)
            state["full_plan_mirrored"] = True
            state["full_plan_mirror_meta"] = meta
            save_state(state)
            _log(f"full plan mirror done: {meta}")

        for b in plan["batches"]:
            bi = b["batch_index"]
            if bi < start_batch:
                continue
            published = published_stems_set(hcfg)
            stems = [c["stem"] for c in b["closes"]]
            to_import = [s for s in stems if s not in published]

            if state.get("pending_readback_batch_index") == bi:
                _log(f"resume readback-only batch {bi}")
                clear_cache()
                rows = optimized_readback(hcfg, stems)
                write_batch_parity_csv(bi, rows)
                state["pending_readback_batch_index"] = None
                state["last_verified_batch_index"] = bi
                state["verified_close_count"] = state.get("verified_close_count", 0) + len(stems)
                save_state(state)
                continue

            backup = OUT / "apr_sep_backups" / f"batch_{bi:04d}"
            if to_import:
                from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_runner import (
                    BatchSpec,
                    validate_batch,
                )

                mirror_batch_stems(stems, backup)
                spec = BatchSpec(
                    month=MONTH,
                    combo_folder=COMBO,
                    stems=tuple(stems),
                    pipeline_line_id=PIPELINE_LINE,
                    import_batch_id=b["import_batch_id"],
                )
                val = validate_batch(APR_SEP, spec)
                if val.get("errors"):
                    raise RuntimeError(f"validate_batch {bi}: {val['errors'][:3]}")
                _log(f"import batch {bi} n={len(to_import)}")
                outcomes, wall = run_import_batch(stems, b["import_batch_id"])
                state["last_completed_batch_index"] = bi
                save_state(state)
                _log(f"import batch {bi} wall={wall:.1f}s outcomes={len(outcomes)}")
            else:
                _log(f"import batch {bi} skip all published (idempotent)")

            clear_cache()
            try:
                rows = optimized_readback(hcfg, stems)
            except Exception as exc:
                state["pending_readback_batch_index"] = bi
                save_state(state)
                _log(f"readback failed batch {bi}: {exc} — pending readback, NO re-import")
                raise
            write_batch_parity_csv(bi, rows)
            published = published_stems_set(hcfg)
            ledger = ch_ledger(hcfg)
            if ledger["sep_window"] != sep_before:
                raise RuntimeError("September window changed — STOP")
            state["last_verified_batch_index"] = bi
            state["pending_readback_batch_index"] = None
            state["verified_close_count"] = state.get("verified_close_count", 0) + len(stems)
            save_state(state)

        ledger = ch_ledger(hcfg)
        if ledger["may_published"] == EXPECTED_MAY:
            state["final_verdict"] = "MAY_FULL_PUBLISH_COMPLETE"
        else:
            state["final_verdict"] = "MAY_FULL_PUBLISH_PARTIAL"
        save_state(state)
        VERDICT_PATH.write_text(f"# Verdict\n\n**{state['final_verdict']}**\n", encoding="utf-8")
    except Exception as exc:
        exit_code = 1
        state["final_verdict"] = "MAY_FULL_PUBLISH_FAILED"
        save_state(state)
        _log(f"FAILED: {exc}")
    finally:
        EXIT_PATH.write_text(str(exit_code), encoding="utf-8")
        release_pid_file()
        if exit_code != 0 and PID_PATH.is_file():
            release_pid_file()
    return exit_code


def clear_cache() -> None:
    from research_charts.historical_pool_state_v1 import clear_published_closes_cache

    clear_published_closes_cache()


def write_prep_docs(runner_path: Path) -> None:
    log_path = OUT / "nohup_run.log"
    cmd = (
        f"nohup python3 -u {runner_path} --execute --resume "
        f"> {log_path} 2>&1 < /dev/null &"
    )
    (OUT / "nohup_start_command.sh").write_text(
        f"""#!/bin/bash
# After explicit approval:
# export {APPROVAL_ENV}=1
{cmd}
echo "pid=$! written to {PID_PATH}"
""",
        encoding="utf-8",
    )
    (OUT / "status_command.sh").write_text(
        f"#!/bin/bash\npython3 -u {runner_path} --status\n",
        encoding="utf-8",
    )
    (OUT / "resume_command.sh").write_text(
        f"""#!/bin/bash
# Readback-only or continue from progress.json
export {APPROVAL_ENV}=1
nohup python3 -u {runner_path} --execute --resume > {log_path} 2>&1 < /dev/null &
""",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 4X-AL May nohup runner")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    runner_path = Path(__file__).resolve()
    write_prep_docs(runner_path)

    if not any([args.preflight, args.dry_run, args.execute, args.status]):
        parser.print_help()
        return 2

    hcfg = _setup()
    plan = load_batch_plan()

    if args.status:
        return cmd_status(hcfg, plan)
    if args.preflight:
        return cmd_preflight(hcfg, plan)
    if args.dry_run:
        return cmd_dry_run(hcfg, plan)
    if args.execute:
        return cmd_execute(hcfg, plan, resume=args.resume)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Phase 4X-AO — Jun–Sep 2026 5m PD1 density validation + nohup publish.

CH writes only with: --execute AND JUN_SEP_NOHUP_EXECUTE_APPROVED=1
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
OUT = ROOT / "results/phase4xao_jun_sep_nohup_rollout_v1"
TOOL = Path(__file__).resolve().parent

SPREAD_DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
POOLS_DASH = ROOT / "backtester/short/dashboard"
TRP = Path("/home/telgenbuescher/projects/trading_research_platform")
LMV2 = POOLS_DASH / "research_charts/live_micro_lld_v2_ch_test"
APR_SEP = LMV2 / "doge_apr_sep_2026_v1_history"
MULTI = LMV2 / "doge_multi_month_2026_history/production/results"
SEP_STAGING = (
    LMV2 / "doge_multi_month_2026_history/staging/phase6xt_5m_pd1_jan_sep_v1/results/2026-09/5m_PD1"
)

PIPELINE_LINE = "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1"
COMBO = "5m_PD1"
BATCH_SIZE = 50
BATCH_NS = uuid.UUID("7c9e6679-7425-40de-944b-e07fc1f90ae7")

MONTHS_ORDER = ("2026-06", "2026-07", "2026-08", "2026-09")
EXPECTED_FROZEN = {"2026-06": 8640, "2026-07": 8928, "2026-08": 8928, "2026-09": 8640}
EXPECTED_MAY = 8928
SEP_WINDOW_START = "2026-09-09"
SEP_WINDOW_END = "2026-09-18"
SEP_WINDOW_EXPECTED = 2592

STATE_PATH = OUT / "progress.json"
RESUME_STATE_PATH = OUT / "resume_state.json"
PID_PATH = OUT / "pid"
EXIT_PATH = OUT / "exit_code.txt"
APPROVAL_ENV = "JUN_SEP_NOHUP_EXECUTE_APPROVED"
LOG_PATH = OUT / "nohup_run.log"


def _setup() -> Any:
    sys.path.insert(0, str(TOOL))
    from import_path_5m_pd1 import setup_hcfg

    return setup_hcfg()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "runner.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def frozen_source_dir(month: str) -> Path:
    if month == "2026-09":
        if SEP_STAGING.is_dir() and len(list(SEP_STAGING.glob("*.json"))) == EXPECTED_FROZEN[month]:
            return SEP_STAGING
        d = MULTI / month / COMBO
        if d.is_dir() and len(list(d.glob("*.json"))) == EXPECTED_FROZEN[month]:
            return d
        raise FileNotFoundError(f"no 8640-file September frozen source")
    d = MULTI / month / COMBO
    if not d.is_dir():
        raise FileNotFoundError(f"missing frozen dir {d}")
    return d


def month_range_sql(month: str) -> tuple[str, str]:
    y, m = int(month[:4]), int(month[5:7])
    start = f"{y:04d}-{m:02d}-01"
    if m == 12:
        end = f"{y + 1:04d}-01-01"
    else:
        end = f"{y:04d}-{m + 1:02d}-01"
    return start, end


def ch_ledger(hcfg: Any) -> dict[str, Any]:
    import clickhouse_connect
    from research_charts.clickhouse_config import load_clickhouse_config
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.publish_schema import TABLE_PUBLISH

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database
    lid, ch = hcfg.pipeline_line_id, hcfg.config_hash
    try:
        by_month = {}
        for mo in ("2026-05",) + tuple(MONTHS_ORDER):
            s, e = month_range_sql(mo)
            n = client.query(
                f"""
                SELECT count() FROM {db}.{TABLE_PUBLISH} FINAL
                WHERE pipeline_line_id = {{lid:String}} AND config_hash = {{ch:String}}
                  AND publish_complete = 1
                  AND decision_close >= {{s:String}} AND decision_close < {{e:String}}
                """,
                parameters={"lid": lid, "ch": ch, "s": s, "e": e},
            ).result_rows[0][0]
            by_month[mo] = int(n)
        sep_win = client.query(
            f"""
            SELECT count() FROM {db}.{TABLE_PUBLISH} FINAL
            WHERE pipeline_line_id = {{lid:String}} AND config_hash = {{ch:String}}
              AND publish_complete = 1
              AND decision_close >= {{s:String}} AND decision_close < {{e:String}}
            """,
            parameters={"lid": lid, "ch": ch, "s": SEP_WINDOW_START, "e": SEP_WINDOW_END},
        ).result_rows[0][0]
        return {"by_month": by_month, "sep_window": int(sep_win), "may": by_month["2026-05"]}
    finally:
        client.close()


def published_stems_month(hcfg: Any, month: str) -> set[str]:
    import clickhouse_connect
    from research_charts.clickhouse_config import load_clickhouse_config
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.publish_schema import TABLE_PUBLISH

    s, e = month_range_sql(month)
    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database
    try:
        rows = client.query(
            f"""
            SELECT decision_close FROM {db}.{TABLE_PUBLISH} FINAL
            WHERE pipeline_line_id = {{lid:String}} AND config_hash = {{ch:String}}
              AND publish_complete = 1
              AND decision_close >= {{s:String}} AND decision_close < {{e:String}}
            """,
            parameters={"lid": hcfg.pipeline_line_id, "ch": hcfg.config_hash, "s": s, "e": e},
        ).result_rows
        out: set[str] = set()
        for (dc,) in rows:
            if dc.tzinfo is None:
                dc = dc.replace(tzinfo=timezone.utc)
            out.add(dc.strftime("%Y%m%dT%H%M%SZ"))
        return out
    finally:
        client.close()


def build_month_plan(month: str) -> dict[str, Any]:
    frozen = sorted(p.stem for p in frozen_source_dir(month).glob("*.json"))
    if len(frozen) != EXPECTED_FROZEN[month]:
        raise RuntimeError(f"{month} frozen {len(frozen)} != {EXPECTED_FROZEN[month]}")
    batches = []
    for i in range(0, len(frozen), BATCH_SIZE):
        chunk = frozen[i : i + BATCH_SIZE]
        bi = i // BATCH_SIZE
        batches.append(
            {
                "batch_index": bi,
                "import_batch_id": str(uuid.uuid5(BATCH_NS, f"{month}:{bi}")),
                "closes": [{"stem": s} for s in chunk],
            }
        )
    return {"month": month, "batch_size": BATCH_SIZE, "close_count": len(frozen), "batches": batches}


def load_state() -> dict[str, Any]:
    if STATE_PATH.is_file():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {
        "months": {},
        "current_month": MONTHS_ORDER[0],
        "may_at_start": None,
        "sep_window_at_start": None,
        "final_verdict": None,
    }


def save_state(state: dict[str, Any]) -> None:
    state["updated_at_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    OUT.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    RESUME_STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def execute_approved() -> bool:
    return os.environ.get(APPROVAL_ENV, "").strip() == "1"


def _pgrep_python_runner(needle: str) -> list[str]:
    r = subprocess.run(["pgrep", "-f", needle], capture_output=True, text=True)
    out: list[str] = []
    me = os.getpid()
    for tok in (r.stdout or "").split():
        if not tok.isdigit():
            continue
        pid = int(tok)
        if pid == me:
            continue
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
        except OSError:
            continue
        if "python" in cmd and needle in cmd and " -c " not in cmd:
            out.append(f"{pid} {cmd.strip()}")
    return out


def _running_lock() -> list[str]:
    out: list[str] = []
    for needle in (
        "run_completion_import",
        "phase4xal_full_may_nohup_runner_v1/run_may_nohup.py",
        "phase4xao_jun_sep_nohup_rollout_v1/run_jun_sep_nohup.py",
    ):
        out.extend(_pgrep_python_runner(needle))
    if PID_PATH.is_file():
        pid = PID_PATH.read_text().strip()
        if pid and pid != str(os.getpid()) and subprocess.run(["kill", "-0", pid], capture_output=True).returncode == 0:
            out.append(f"pid_file:{pid}")
    return out


def acquire_pid() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    if PID_PATH.is_file():
        old = PID_PATH.read_text().strip()
        if old and subprocess.run(["kill", "-0", old], capture_output=True).returncode == 0:
            raise SystemExit(f"runner alive pid={old}")
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")


def release_pid() -> None:
    PID_PATH.unlink(missing_ok=True)


def frozen_preflight_rows() -> list[dict[str, str]]:
    rows = []
    for month in MONTHS_ORDER:
        d = frozen_source_dir(month)
        n = len(list(d.glob("*.json")))
        rows.append(
            {
                "month": month,
                "frozen_dir": str(d),
                "frozen_count": str(n),
                "expected": str(EXPECTED_FROZEN[month]),
                "ok": "1" if n == EXPECTED_FROZEN[month] else "0",
            }
        )
    return rows


def verify_sep_overlap_hashes() -> list[str]:
    errs = []
    apr = APR_SEP / "production/results/2026-09" / COMBO
    if not apr.is_dir():
        return errs
    src = frozen_source_dir("2026-09")
    for p in apr.glob("*.json"):
        stem = p.stem
        s = src / f"{stem}.json"
        if not s.is_file():
            errs.append(f"sep overlap missing staging {stem}")
            continue
        if _sha256_file(p) != _sha256_file(s):
            errs.append(f"sep overlap hash mismatch {stem}")
    return errs


def preflight(hcfg: Any) -> dict[str, Any]:
    blockers: list[str] = []
    ledger = ch_ledger(hcfg)
    if ledger["may"] != EXPECTED_MAY:
        blockers.append(f"may {ledger['may']} != {EXPECTED_MAY}")
    if ledger["sep_window"] != SEP_WINDOW_EXPECTED:
        blockers.append(f"sep_window {ledger['sep_window']} != {SEP_WINDOW_EXPECTED}")
    for month in MONTHS_ORDER:
        try:
            d = frozen_source_dir(month)
            fc = len(list(d.glob("*.json")))
            if fc != EXPECTED_FROZEN[month]:
                blockers.append(f"frozen count {month} {fc} != {EXPECTED_FROZEN[month]}")
        except Exception as exc:
            blockers.append(f"frozen {month}: {exc}")
    overlap_errs = verify_sep_overlap_hashes()
    if overlap_errs:
        blockers.append(f"sep_overlap: {overlap_errs[:3]}")
    if _running_lock():
        blockers.append("concurrent import/runner")
    du = shutil.disk_usage(APR_SEP)
    if du.free < 5 * 1024**3:
        blockers.append(f"disk < 5GiB free")
    if BATCH_SIZE > 200:
        blockers.append("batch_size > 200")
    return {
        "ok": len(blockers) == 0,
        "blockers": blockers,
        "ledger": ledger,
        "disk_free_gb": round(du.free / 1024**3, 2),
        "execute_approved": execute_approved(),
        "remaining_by_month": {
            m: max(0, EXPECTED_FROZEN[m] - ledger["by_month"].get(m, 0)) for m in MONTHS_ORDER
        },
    }


def dry_run(hcfg: Any) -> dict[str, Any]:
    ledger = ch_ledger(hcfg)
    total_would = 0
    per_month = []
    for month in MONTHS_ORDER:
        published = published_stems_month(hcfg, month)
        plan = build_month_plan(month)
        would = sum(1 for b in plan["batches"] for c in b["closes"] if c["stem"] not in published)
        total_would += would
        per_month.append(
            {
                "month": month,
                "batches": len(plan["batches"]),
                "would_import": would,
                "already_published": len(published),
                "target": EXPECTED_FROZEN[month],
            }
        )
    return {
        "would_import_total": total_would,
        "may_published": ledger["may"],
        "sep_window": ledger["sep_window"],
        "months": per_month,
    }


def mirror_stems(month: str, stems: list[str], backup_dir: Path) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    lookup_path = APR_SEP / "production/provenance_lookup.json"
    if not (backup_dir / "provenance_lookup.json.bak").is_file():
        shutil.copy2(lookup_path, backup_dir / "provenance_lookup.json.bak")
    dest = APR_SEP / "production/results" / month / COMBO
    dest.mkdir(parents=True, exist_ok=True)
    src_root = frozen_source_dir(month)
    lookup = json.loads(lookup_path.read_text(encoding="utf-8"))
    for stem in stems:
        src = src_root / f"{stem}.json"
        dst = dest / f"{stem}.json"
        if not dst.is_file():
            shutil.copy2(src, dst)
        elif _sha256_file(src) != _sha256_file(dst):
            raise RuntimeError(f"hash conflict {month}/{stem}")
        rel = f"production/results/{month}/{COMBO}/{stem}.json"
        fid = str(json.loads(src.read_text(encoding="utf-8")).get("frozen_input", {}).get("snapshot_id") or "")
        lookup.setdefault("entries", {})[rel] = {"source_frozen_id": fid, "dest_sha256": _sha256_file(dst)}
    lookup["entry_count"] = len(lookup.get("entries") or {})
    lookup_path.write_text(json.dumps(lookup, indent=2, sort_keys=True), encoding="utf-8")
    from research_charts.historical_lmv2 import provenance_lookup as _pl

    _pl._LOOKUP_CACHE.clear()


def mirror_month_remaining(month: str, published: set[str], plan: dict[str, Any]) -> dict[str, int]:
    all_stems = [c["stem"] for b in plan["batches"] for c in b["closes"]]
    todo = [s for s in all_stems if s not in published]
    backup = OUT / "apr_sep_backups" / f"{month}_full_mirror"
    for i in range(0, len(todo), 200):
        mirror_stems(month, todo[i : i + 200], backup)
        _log(f"mirror {month} {min(i+200, len(todo))}/{len(todo)}")
    return {"mirrored": len(todo), "total": len(all_stems)}


def run_import_batch(month: str, stems: list[str], batch_id: str) -> tuple[list[dict], float]:
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_runner import BatchSpec
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_write import execute_batch_import

    os.environ["HISTORICAL_JSON_IMPORT_ALLOW_WRITE"] = "1"
    os.environ["HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE"] = "1"
    spec = BatchSpec(
        month=month,
        combo_folder=COMBO,
        stems=tuple(stems),
        pipeline_line_id=PIPELINE_LINE,
        import_batch_id=batch_id,
    )
    t0 = time.perf_counter()
    outcomes = execute_batch_import(APR_SEP, spec, test_run_id=str(uuid.uuid4()))
    return outcomes, time.perf_counter() - t0


def clear_caches() -> None:
    from import_path_5m_pd1 import ensure_5m_pd1_readback_import_path

    ensure_5m_pd1_readback_import_path()
    from research_charts.historical_pool_state_v1 import clear_published_closes_cache

    clear_published_closes_cache()


def optimized_readback(hcfg: Any, month: str, stems: list[str]) -> list[dict[str, Any]]:
    from import_path_5m_pd1 import ensure_5m_pd1_readback_import_path

    ensure_5m_pd1_readback_import_path()
    import clickhouse_connect
    from research_charts.clickhouse_config import load_clickhouse_config
    from research_charts.historical_pool_state_batch_reader_v1 import build_episode_walk_batch_cache
    from research_charts.historical_pool_state_v1 import clear_published_closes_cache
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import PublishedActiveReader

    from readback_gate import gate_ok, optimized_readback_batch, parity_index

    clear_published_closes_cache()
    density_ref = parity_index(month)
    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    database = cfg.database
    closes_dt = [datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc) for s in stems]
    cache = build_episode_walk_batch_cache(client, database, PIPELINE_LINE, hcfg.config_hash, closes_dt)
    reader = PublishedActiveReader()
    rows = optimized_readback_batch(hcfg, month, stems, density_ref, cache, client, database, reader)
    client.close()
    if not gate_ok(rows):
        raise RuntimeError(f"readback gate failed {month} {stems[0]}..{stems[-1]}")
    return rows


def append_progress_csv(row: dict[str, Any]) -> None:
    path = OUT / "monthly_publish_progress.csv"
    fields = list(row.keys())
    exists = path.is_file()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            w.writeheader()
        w.writerow(row)


def write_parity_batch(month: str, bi: int, rows: list[dict[str, Any]]) -> None:
    p = OUT / "parity_by_batch" / month / f"batch_{bi:04d}.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def month_state(state: dict[str, Any], month: str) -> dict[str, Any]:
    state.setdefault("months", {})
    if month not in state["months"]:
        state["months"][month] = {
            "validation": "PENDING",
            "publish": "PENDING",
            "last_verified_batch_index": -1,
            "pending_readback_batch_index": None,
            "verified_close_count": 0,
        }
    return state["months"][month]


def run_month_validation(month: str, state: dict[str, Any], resume: bool) -> None:
    ms = month_state(state, month)
    if ms.get("validation") == "MONTH_VALIDATION_PASS":
        return
    from month_validate import validate_month

    out_dir = OUT / "month_validations" / month
    _log(f"validation start {month}")
    if month == "2026-09":
        from september_immutable_binding_v1 import BINDING_ENV, binding_env_enabled, load_binding_spec

        if binding_env_enabled():
            spec = load_binding_spec()
            _log(
                f"September immutable binding ACTIVE env={BINDING_ENV} "
                f"snapshot_id={spec['snapshot_id'][:16]}…"
            )
        else:
            _log(
                f"September immutable binding INACTIVE (set {BINDING_ENV}=1 for 4X-AP staging snapshot)"
            )
    result = validate_month(
        month,
        frozen_source_dir(month),
        out_dir,
        resume=resume or ms.get("validation") == "IN_PROGRESS",
        log_fn=_log,
    )
    row = {"month": month, **{k: str(v) for k, v in result.items()}}
    path = OUT / "monthly_density_validation.csv"
    exists = path.is_file()
    with path.open("a", newline="", encoding="utf-8") as f:
        fields = list(row.keys())
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            w.writeheader()
        w.writerow(row)
    if result["verdict"] != "MONTH_VALIDATION_PASS":
        ms["validation"] = "MONTH_VALIDATION_BLOCKED"
        save_state(state)
        raise RuntimeError(f"{month} validation blocked: {result}")
    ms["validation"] = "MONTH_VALIDATION_PASS"
    save_state(state)
    _log(f"validation PASS {month}")


def publish_month(hcfg: Any, month: str, state: dict[str, Any], resume: bool) -> None:
    from research_charts.live_micro_lld_v2_ch_test.historical_json_import.batch_runner import (
        BatchSpec,
        validate_batch,
    )

    ms = month_state(state, month)
    if ms.get("publish") == "MONTH_PUBLISH_PASS":
        return
    plan = build_month_plan(month)
    may_before = ch_ledger(hcfg)["may"]
    sep_before = ch_ledger(hcfg)["sep_window"]
    published = published_stems_month(hcfg, month)
    if not ms.get("full_mirror_done"):
        meta = mirror_month_remaining(month, published, plan)
        ms["full_mirror_done"] = True
        ms["mirror_meta"] = meta
        save_state(state)
        _log(f"mirror done {month} {meta}")

    start_bi = 0 if not resume else max(0, int(ms.get("last_verified_batch_index", -1)) + 1)
    if ms.get("pending_readback_batch_index") is not None:
        start_bi = int(ms["pending_readback_batch_index"])

    for b in plan["batches"]:
        bi = b["batch_index"]
        if bi < start_bi:
            continue
        published = published_stems_month(hcfg, month)
        stems = [c["stem"] for c in b["closes"]]
        to_import = [s for s in stems if s not in published]
        skip_import = [s for s in stems if s in published]

        if ms.get("pending_readback_batch_index") == bi:
            _log(f"readback-only {month} batch {bi}")
            clear_caches()
            rows = optimized_readback(hcfg, month, stems)
            write_parity_batch(month, bi, rows)
            ms["pending_readback_batch_index"] = None
            ms["last_verified_batch_index"] = bi
            ms["verified_close_count"] = int(ms.get("verified_close_count", 0)) + len(stems)
            save_state(state)
            continue

        backup = OUT / "apr_sep_backups" / f"{month}_batch_{bi:04d}"
        if skip_import:
            from mixed_batch_import_v1 import assert_published_stems_integrity

            assert_published_stems_integrity(
                catalog_root=APR_SEP,
                month=month,
                combo_folder=COMBO,
                pipeline_line_id=PIPELINE_LINE,
                config_hash=hcfg.config_hash,
                published_stems=published,
                batch_stems=stems,
            )
        if to_import:
            mirror_stems(month, to_import, backup)
            spec = BatchSpec(
                month=month,
                combo_folder=COMBO,
                stems=tuple(to_import),
                pipeline_line_id=PIPELINE_LINE,
                import_batch_id=b["import_batch_id"],
            )
            val = validate_batch(APR_SEP, spec)
            if val.get("errors"):
                raise RuntimeError(f"validate {month} batch {bi}: {val['errors'][:3]}")
            _log(
                f"import {month} batch {bi} n={len(to_import)} "
                f"skip={len(skip_import)} readback_stems={len(stems)}"
            )
            outcomes, wall = run_import_batch(month, to_import, b["import_batch_id"])
            _log(f"import {month} batch {bi} wall={wall:.1f}s n={len(outcomes)}")
        else:
            _log(f"skip import {month} batch {bi} (idempotent)")

        clear_caches()
        try:
            rows = optimized_readback(hcfg, month, stems)
        except Exception as exc:
            ms["pending_readback_batch_index"] = bi
            save_state(state)
            append_progress_csv(
                {"month": month, "batch": bi, "status": "readback_pending", "error": str(exc)[:200]}
            )
            raise
        write_parity_batch(month, bi, rows)
        ledger = ch_ledger(hcfg)
        if ledger["may"] != may_before:
            raise RuntimeError("May publish count changed — STOP")
        if ledger["sep_window"] != sep_before:
            raise RuntimeError("September pilot window changed — STOP")
        ms["last_verified_batch_index"] = bi
        ms["pending_readback_batch_index"] = None
        ms["verified_close_count"] = int(ms.get("verified_close_count", 0)) + len(stems)
        save_state(state)
        append_progress_csv(
            {
                "month": month,
                "batch": bi,
                "status": "verified",
                "published_month": ledger["by_month"].get(month, 0),
            }
        )

    ledger = ch_ledger(hcfg)
    if ledger["by_month"].get(month, 0) != EXPECTED_FROZEN[month]:
        raise RuntimeError(f"{month} published {ledger['by_month'].get(month)} != target")
    ms["publish"] = "MONTH_PUBLISH_PASS"
    save_state(state)
    _log(f"MONTH_PUBLISH_PASS {month}")


def cmd_execute(hcfg: Any, resume: bool) -> int:
    if not execute_approved():
        _log(f"REFUSED: set {APPROVAL_ENV}=1")
        EXIT_PATH.write_text("2", encoding="utf-8")
        return 2
    if _running_lock():
        _log("REFUSED: concurrent runner")
        return 2
    acquire_pid()
    state = load_state()
    exit_code = 0
    try:
        pf = preflight(hcfg)
        (OUT / "preflight_result.json").write_text(json.dumps(pf, indent=2), encoding="utf-8")
        if not pf["ok"]:
            raise RuntimeError(f"preflight: {pf['blockers']}")
        if state["may_at_start"] is None:
            state["may_at_start"] = pf["ledger"]["may"]
            state["sep_window_at_start"] = pf["ledger"]["sep_window"]
            save_state(state)

        start_month_idx = 0
        if resume and state.get("current_month") in MONTHS_ORDER:
            start_month_idx = MONTHS_ORDER.index(state["current_month"])

        for month in MONTHS_ORDER[start_month_idx:]:
            state["current_month"] = month
            save_state(state)
            run_month_validation(month, state, resume=resume)
            publish_month(hcfg, month, state, resume=resume)

        ledger = ch_ledger(hcfg)
        complete = all(ledger["by_month"].get(m, 0) == EXPECTED_FROZEN[m] for m in MONTHS_ORDER)
        if complete and ledger["may"] == EXPECTED_MAY:
            state["final_verdict"] = "JUN_SEP_DENSITY_PUBLISH_COMPLETE"
        else:
            state["final_verdict"] = "JUN_SEP_DENSITY_PUBLISH_PARTIAL"
        save_state(state)
        (OUT / "final_summary.json").write_text(
            json.dumps({"verdict": state["final_verdict"], "ledger": ledger}, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        exit_code = 1
        if "MONTH_VALIDATION_BLOCKED" in str(exc):
            state["final_verdict"] = "JUN_SEP_DENSITY_PUBLISH_BLOCKED"
        else:
            state["final_verdict"] = "JUN_SEP_DENSITY_PUBLISH_PARTIAL"
        save_state(state)
        _log(f"FAILED: {exc}")
        fail_path = OUT / "readback_failures.csv"
        with fail_path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["utc", "error"])
            if fail_path.stat().st_size == 0:
                w.writeheader()
            w.writerow({"utc": datetime.now(timezone.utc).isoformat(), "error": str(exc)[:1000]})
    finally:
        EXIT_PATH.write_text(str(exit_code), encoding="utf-8")
        release_pid()
    return exit_code


def write_artifact_csvs() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = frozen_preflight_rows()
    with (OUT / "frozen_preflight_by_month.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 4X-AO Jun–Sep nohup rollout")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    if not any([args.preflight, args.dry_run, args.execute, args.status]):
        parser.print_help()
        return 2

    hcfg = _setup()
    if args.status:
        print(json.dumps({"state": load_state(), "ledger": ch_ledger(hcfg)}, indent=2))
        return 0
    pf = preflight(hcfg)
    (OUT / "preflight_result.json").write_text(json.dumps(pf, indent=2), encoding="utf-8")
    dr = dry_run(hcfg)
    (OUT / "dry_run_result.json").write_text(json.dumps(dr, indent=2), encoding="utf-8")
    write_artifact_csvs()
    if args.preflight:
        _log(f"preflight ok={pf['ok']}")
        print(json.dumps(pf, indent=2))
        return 0 if pf["ok"] else 1
    if args.dry_run:
        _log(f"dry_run would_import={dr['would_import_total']}")
        print(json.dumps(dr, indent=2))
        return 0
    if args.execute:
        return cmd_execute(hcfg, resume=args.resume)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

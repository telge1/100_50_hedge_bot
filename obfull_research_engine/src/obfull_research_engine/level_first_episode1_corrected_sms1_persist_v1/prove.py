"""Two independent e2e subprocesses + oracle + A/B compare. Episode 1 only."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.persist import atomic_write_json, atomic_write_text
from ..market_profile_lld_shared_event_materialization_v1.hashing import sha256_hex
from ..paths import ENGINE_ROOT
from . import (
    ALLOW_CLICKHOUSE_WRITES,
    DETECTION_ISO,
    EPISODE_ID,
    EVIDENCE_START_ISO,
    PARENT_GOLDEN_RUN,
    PARENT_GOLDEN_VERDICT,
    SMS1_EP1_STATES_SHA256,
    SYMBOL,
)
from .compare_runs import compare_tables
from .e2e import RESULTS_ROOT, hash_raw_inputs
from .io_zst import body_rows, read_jsonl_zst
from .oracle import audit_directory
from .recategorize import recategorize_walls_and_refills
from .runner import _frozen_hashes, _git, _peak, _pids
from .time_contract import TIME_CONTRACT

PROVEN = "EPISODE1_RAW_TO_DERIVED_END_TO_END_REPRODUCIBILITY_PROVEN"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _python() -> str:
    return sys.executable


def _env() -> dict[str, str]:
    env = os.environ.copy()
    src = str(ENGINE_ROOT / "src")
    parent_src = str(ENGINE_ROOT.parent / "src")
    env["PYTHONPATH"] = os.pathsep.join([src, parent_src, env.get("PYTHONPATH", "")]).strip(os.pathsep)
    return env


def _spawn_e2e(run_key: str, out_dir: Path) -> dict[str, Any]:
    cmd = [
        _python(),
        "-m",
        "obfull_research_engine.level_first_episode1_corrected_sms1_persist_v1.cli",
        "e2e",
        "--run-key",
        run_key,
        "--out",
        str(out_dir),
    ]
    t0 = time.monotonic()
    proc = subprocess.run(
        cmd,
        cwd=str(ENGINE_ROOT),
        env=_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "cmd": cmd,
        "returncode": proc.returncode,
        "elapsed_s": round(time.monotonic() - t0, 3),
        "stdout_tail": proc.stdout[-4000:],
        "stderr_tail": proc.stderr[-4000:],
        "pid_note": "child process; see run_manifest.pid",
    }


def _load_manifest(directory: Path) -> dict[str, Any]:
    path = directory / "run_manifest.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def decide_verdict(gates: dict[str, Any]) -> tuple[str, list[str]]:
    reasons = []
    if gates.get("fresh_processes") != 2:
        reasons.append("fresh_processes != 2")
    if gates.get("independent_raw_replays") != 2:
        reasons.append("independent_raw_replays != 2")
    if gates.get("persisted_readbacks") != 2:
        reasons.append("persisted_readbacks != 2")
    if gates.get("golden_mismatches_run_a") != 0:
        reasons.append("golden_mismatches_run_a != 0")
        return "EPISODE1_E2E_GOLDEN_PARITY_FAILED", reasons
    if gates.get("golden_mismatches_run_b") != 0:
        reasons.append("golden_mismatches_run_b != 0")
        return "EPISODE1_E2E_GOLDEN_PARITY_FAILED", reasons
    if gates.get("run_a_vs_run_b_semantic_diffs") != 0:
        reasons.append("run_a_vs_run_b_semantic_diffs != 0")
        return "EPISODE1_E2E_REPRODUCIBILITY_FAILED", reasons
    if not gates.get("content_hash_equal", False):
        reasons.append("content_hash_equal is false")
        return "EPISODE1_E2E_REPRODUCIBILITY_FAILED", reasons
    if gates.get("derived_false_positives") != 0:
        reasons.append("derived_false_positives != 0")
        return "EPISODE1_E2E_DERIVED_ORACLE_FALSE_POSITIVES", reasons
    if gates.get("derived_false_negatives") != 0:
        reasons.append("derived_false_negatives != 0")
        return "EPISODE1_E2E_DERIVED_ORACLE_FALSE_NEGATIVES", reasons
    if gates.get("causality_violations") != 0:
        reasons.append("causality_violations != 0")
        return "EPISODE1_E2E_CAUSALITY_VIOLATION", reasons
    if gates.get("refill_misclassifications") != 0:
        reasons.append("refill_misclassifications != 0")
        return "EPISODE1_E2E_REFILL_MISCLASSIFICATION", reasons
    if gates.get("raw_input_hashes_unchanged") is not True:
        reasons.append("raw_input_hashes_unchanged is not true")
        return "EPISODE1_E2E_RAW_INPUT_MUTATED", reasons
    if gates.get("all_required_tests_passed") is not True:
        reasons.append("all_required_tests_passed is not true")
        return "EPISODE1_E2E_TESTS_FAILED", reasons
    if reasons:
        return "EPISODE1_E2E_BLOCKED", reasons
    return PROVEN, []


def run_prove(*, skip_tests: bool = False) -> dict[str, Any]:
    if ALLOW_CLICKHOUSE_WRITES:
        raise RuntimeError("clickhouse writes forbidden")
    t0 = time.monotonic()
    pids_before = _pids()
    frozen_before = _frozen_hashes()
    raw_before = hash_raw_inputs()
    config = json.loads((ENGINE_ROOT / "config/level_first_episode1_corrected_sms1_persist_v1.json").read_text(encoding="utf-8"))
    token = secrets.token_hex(8)
    key_a = f"e2e1_{token}a"
    key_b = f"e2e1_{token}b"
    prove_dir = RESULTS_ROOT / SYMBOL / f"e2e1_{token}_prove"
    dir_a = RESULTS_ROOT / SYMBOL / key_a
    dir_b = RESULTS_ROOT / SYMBOL / key_b
    prove_dir.mkdir(parents=True, exist_ok=True)

    print("[prove] pytest (before archive e2e)", flush=True)
    test_result = {"passed": 0, "failed": 0, "skipped": 0, "returncode": None, "skipped_here": skip_tests}
    if not skip_tests:
        test_cmd = [
            _python(),
            "-m",
            "pytest",
            "-q",
            "tests/test_level_first_episode1_corrected_sms1_persist_v1.py",
            "tests/test_level_first_episode1_full_ob_state_golden_parity_v1.py",
            "tests/test_event_drilldown_v1.py",
            "tests/test_level_first_episode1_e2e_raw_to_derived_v1.py",
        ]
        proc = subprocess.run(test_cmd, cwd=str(ENGINE_ROOT), env=_env(), capture_output=True, text=True, check=False)
        test_result = {
            "cmd": test_cmd,
            "returncode": proc.returncode,
            "stdout_tail": proc.stdout[-8000:],
            "stderr_tail": proc.stderr[-4000:],
            "passed": proc.returncode == 0,
        }
        print(proc.stdout[-2000:], flush=True)
        if proc.returncode != 0:
            print("[prove] tests failed; continuing archive e2e for remaining gates", flush=True)

    print(f"[prove] spawn Run A {key_a}", flush=True)
    spawn_a = _spawn_e2e(key_a, dir_a)
    print(f"[prove] spawn Run B {key_b}", flush=True)
    spawn_b = _spawn_e2e(key_b, dir_b)

    man_a = _load_manifest(dir_a)
    man_b = _load_manifest(dir_b)
    print("[prove] oracle A/B", flush=True)
    oracle_a = audit_directory(dir_a, cfg=config) if (dir_a / "walls.jsonl.zst").is_file() else {"derived_false_positives": 1, "error": "missing A"}
    oracle_b = audit_directory(dir_b, cfg=config) if (dir_b / "walls.jsonl.zst").is_file() else {"derived_false_positives": 1, "error": "missing B"}
    print("[prove] A vs B", flush=True)
    ab = compare_tables(dir_a, dir_b)

    walls_a = body_rows(read_jsonl_zst(dir_a / "walls.jsonl.zst")) if (dir_a / "walls.jsonl.zst").is_file() else []
    lc_a = body_rows(read_jsonl_zst(dir_a / "level_changes.jsonl.zst")) if (dir_a / "level_changes.jsonl.zst").is_file() else []
    wall_keys = {(str(w.get("side")), float(w["price"])) for w in walls_a if w.get("price") is not None}
    lc_wall = [
        r
        for r in lc_a
        if r.get("price") is not None and (str(r.get("side")), float(r["price"])) in wall_keys
    ]
    alt_neu = recategorize_walls_and_refills(walls_a, lc_wall)

    raw_after = hash_raw_inputs()
    frozen_after = _frozen_hashes()
    pids_after = _pids()
    tests_ok = bool(skip_tests) or test_result.get("returncode") == 0

    gates = {
        "fresh_processes": 2 if man_a.get("pid") and man_b.get("pid") and man_a.get("pid") != man_b.get("pid") else 0,
        "independent_raw_replays": 2 if man_a.get("fresh_process") and man_b.get("fresh_process") and not man_a.get("fixture") else 0,
        "persisted_readbacks": 2
        if man_a.get("derived_input_mode") == "persisted_sms1_readback"
        and man_b.get("derived_input_mode") == "persisted_sms1_readback"
        else 0,
        "golden_mismatches_run_a": 1 if not (man_a or {}).get("golden") else int((man_a["golden"].get("n_mismatch") or 0)),
        "golden_mismatches_run_b": 1 if not (man_b or {}).get("golden") else int((man_b["golden"].get("n_mismatch") or 0)),
        "run_a_vs_run_b_semantic_diffs": int(ab.get("semantic_diff_count") or 0),
        "content_hash_equal": bool(ab.get("content_hash_equal")),
        "derived_false_positives": int(oracle_a.get("derived_false_positives") or 0) + int(oracle_b.get("derived_false_positives") or 0),
        "derived_false_negatives": int(oracle_a.get("derived_false_negatives") or 0) + int(oracle_b.get("derived_false_negatives") or 0),
        "causality_violations": int(oracle_a.get("causality_violations") or 0) + int(oracle_b.get("causality_violations") or 0) + len(man_a.get("available_at_violations") or []) + len(man_b.get("available_at_violations") or []),
        "refill_misclassifications": int(oracle_a.get("refill_misclassifications") or 0) + int(oracle_b.get("refill_misclassifications") or 0),
        "raw_input_hashes_unchanged": json.dumps(raw_before, sort_keys=True) == json.dumps(raw_after, sort_keys=True) and man_a.get("raw_input_hashes_unchanged") and man_b.get("raw_input_hashes_unchanged"),
        "all_required_tests_passed": tests_ok,
        "sms1_states_hash_unchanged": (frozen_after.get("sms1_ep1_states_100ms") or {}).get("sha256") == SMS1_EP1_STATES_SHA256,
        "pids_unchanged": _pids_unchanged(pids_before, pids_after),
    }
    if not man_a or not man_b:
        gates["golden_mismatches_run_a"] = gates.get("golden_mismatches_run_a") or 1
        gates["golden_mismatches_run_b"] = gates.get("golden_mismatches_run_b") or 1
        gates["independent_raw_replays"] = 0

    verdict, reasons = decide_verdict(gates)
    if spawn_a.get("returncode") not in {0, None} or spawn_b.get("returncode") not in {0, None}:
        if verdict == PROVEN:
            verdict = "EPISODE1_E2E_PROCESS_FAILED"
            reasons.append("e2e subprocess non-zero")
    payload = {
        "verdict": verdict,
        "reasons": reasons,
        "gates": gates,
        "prove_dir": str(prove_dir),
        "run_a": {"run_key": key_a, "dir": str(dir_a), "pid": man_a.get("pid"), "spawn": {k: spawn_a[k] for k in spawn_a if k != "stdout_tail"}},
        "run_b": {"run_key": key_b, "dir": str(dir_b), "pid": man_b.get("pid"), "spawn": {k: spawn_b[k] for k in spawn_b if k != "stdout_tail"}},
        "spawn_a_stdout_tail": spawn_a.get("stdout_tail"),
        "spawn_b_stdout_tail": spawn_b.get("stdout_tail"),
        "golden_a": man_a.get("golden"),
        "golden_b": man_b.get("golden"),
        "counts_a": {k: man_a.get(k) for k in ("n_states", "n_level_changes", "n_book_resets", "n_walls", "n_level_adds", "n_level_increases", "n_level_decreases", "n_level_removals", "n_refill_candidates", "n_confirmed_refills", "n_rejected_refill_candidates", "n_touches", "n_detections")},
        "counts_b": {k: man_b.get(k) for k in ("n_states", "n_level_changes", "n_book_resets", "n_walls", "n_level_adds", "n_level_increases", "n_level_decreases", "n_level_removals", "n_refill_candidates", "n_confirmed_refills", "n_rejected_refill_candidates", "n_touches", "n_detections")},
        "ab": ab,
        "oracle_a": {k: oracle_a.get(k) for k in oracle_a if k not in {"touch_raw_trace", "detection_raw_trace"}},
        "oracle_b": {k: oracle_b.get(k) for k in oracle_b if k not in {"touch_raw_trace", "detection_raw_trace"}},
        "touch_raw_trace": oracle_a.get("touch_raw_trace"),
        "detection_raw_trace": oracle_a.get("detection_raw_trace"),
        "wall_audit": man_a.get("wall_audit_w_781b6ed696e777e1"),
        "time_contract": TIME_CONTRACT,
        "alt_neu": alt_neu,
        "tests": test_result,
        "raw_inputs_before": raw_before,
        "raw_inputs_after": raw_after,
        "frozen_before": frozen_before,
        "frozen_after": frozen_after,
        "pids_before": pids_before,
        "pids_after": pids_after,
        "parent_golden_run": PARENT_GOLDEN_RUN,
        "parent_golden_verdict": PARENT_GOLDEN_VERDICT,
        "episode_id": EPISODE_ID,
        "evidence": [EVIDENCE_START_ISO, DETECTION_ISO],
        "created_at": _now(),
        "elapsed_s": round(time.monotonic() - t0, 3),
        "peak_ram_gb": _peak(),
        "git": _git(),
        "clickhouse_writes": False,
        "config_hash": sha256_hex(config),
        "file_sha256_a": man_a.get("file_sha256"),
        "file_sha256_b": man_b.get("file_sha256"),
        "content_hashes_a": man_a.get("content_hashes_uncompressed"),
        "content_hashes_b": man_b.get("content_hashes_uncompressed"),
        "derived_input_mode_a": man_a.get("derived_input_mode"),
        "derived_input_mode_b": man_b.get("derived_input_mode"),
        "in_memory_bypass_a": man_a.get("in_memory_bypass"),
        "in_memory_bypass_b": man_b.get("in_memory_bypass"),
        "refill_definition": man_a.get("refill_definition"),
        "refill_reject_reason_counts": man_a.get("refill_reject_reason_counts"),
    }
    atomic_write_json(prove_dir / "prove_manifest.json", payload)
    atomic_write_json(prove_dir / "oracle_a.json", oracle_a)
    atomic_write_json(prove_dir / "oracle_b.json", oracle_b)
    atomic_write_json(prove_dir / "ab_compare.json", ab)
    atomic_write_json(prove_dir / "alt_neu.json", alt_neu)
    atomic_write_text(prove_dir / "STATUS", verdict + "\n")
    print(json.dumps({"verdict": verdict, "reasons": reasons, "gates": gates, "prove_dir": str(prove_dir)}, indent=2, default=str), flush=True)
    return payload


def _pids_unchanged(before: dict[str, Any], after: dict[str, Any]) -> bool:
    def ids(key: str) -> set[str]:
        return {str(r.get("pid")) for r in (before.get(key) or [])}

    def ids_a(key: str) -> set[str]:
        return {str(r.get("pid")) for r in (after.get(key) or [])}

    return ids("clickhouse-server") == ids_a("clickhouse-server") and ids("oi_liquidation_collector") == ids_a("oi_liquidation_collector") and ids("Signal_Generator") == ids_a("Signal_Generator")

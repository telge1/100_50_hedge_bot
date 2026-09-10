"""Two fresh independent-derivation e2e subprocesses + oracle + A/B compare."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.persist import atomic_write_json, atomic_write_text
from ..paths import ENGINE_ROOT
from . import ALLOW_CLICKHOUSE_WRITES, SYMBOL
from .compare_runs import compare_tables
from .e2e import RESULTS_ROOT
from .independent_oracle import audit_directory
from .runner import _frozen_hashes, _git, _peak, _pids

PYTHON_BIN = "/home/telgenbuescher/projects/orderbook_analyse/.venv/bin/python"
PROVEN = "EPISODE1_ZONE_TOUCH_DETECTION_RAW_DERIVATION_EVENT_TIME_PROVEN"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = "src:../src"
    return env


def _spawn_e2e(run_key: str, out_dir: Path) -> dict[str, Any]:
    cmd = [
        PYTHON_BIN,
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
        "stdout_tail": proc.stdout[-8000:],
        "stderr_tail": proc.stderr[-4000:],
    }


def _load_manifest(directory: Path) -> dict[str, Any]:
    path = directory / "run_manifest.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _pids_unchanged(before: dict[str, Any], after: dict[str, Any]) -> bool:
    def ids(payload: dict[str, Any], key: str) -> set[str]:
        return {str(row.get("pid")) for row in (payload.get(key) or [])}

    keys = ("clickhouse-server", "oi_liquidation_collector", "Signal_Generator")
    return all(ids(before, key) == ids(after, key) for key in keys)


def decide_verdict(gates: dict[str, Any]) -> tuple[str, list[str]]:
    reasons = []
    if gates.get("fresh_processes") != 2:
        reasons.append("fresh_processes != 2")
    if gates.get("run_a_ok") is not True:
        reasons.append("run_a_ok is false")
    if gates.get("run_b_ok") is not True:
        reasons.append("run_b_ok is false")
    if gates.get("golden_mismatches_run_a") != 0:
        reasons.append("golden_mismatches_run_a != 0")
    if gates.get("golden_mismatches_run_b") != 0:
        reasons.append("golden_mismatches_run_b != 0")
    if gates.get("run_a_vs_run_b_semantic_diffs") != 0:
        reasons.append("run_a_vs_run_b_semantic_diffs != 0")
    if gates.get("content_hash_equal") is not True:
        reasons.append("content_hash_equal is false")
    if gates.get("oracle_false_positives") != 0:
        reasons.append("oracle_false_positives != 0")
    if gates.get("oracle_false_negatives") != 0:
        reasons.append("oracle_false_negatives != 0")
    if gates.get("oracle_look_ahead_violations") != 0:
        reasons.append("oracle_look_ahead_violations != 0")
    if gates.get("raw_input_hashes_unchanged") is not True:
        reasons.append("raw_input_hashes_unchanged is false")
    if gates.get("used_config_first_touch_as_input") is not False:
        reasons.append("used_config_first_touch_as_input is not false")
    if gates.get("used_config_detection_as_input") is not False:
        reasons.append("used_config_detection_as_input is not false")
    if gates.get("independent_verdict_a") != PROVEN:
        reasons.append("independent_verdict_a != event_time_proven")
    if gates.get("independent_verdict_b") != PROVEN:
        reasons.append("independent_verdict_b != event_time_proven")
    if gates.get("live_pids_unchanged") is not True:
        reasons.append("live_pids_unchanged is false")
    return (PROVEN, []) if not reasons else ("EPISODE1_ZONE_TOUCH_DETECTION_RAW_DERIVATION_PROVE_BLOCKED", reasons)


def run_prove_independent() -> dict[str, Any]:
    if ALLOW_CLICKHOUSE_WRITES:
        raise RuntimeError("clickhouse writes forbidden")
    t0 = time.monotonic()
    pids_before = _pids()
    frozen_before = _frozen_hashes()

    token = secrets.token_hex(8)
    key_a = f"ide1_{token}a"
    key_b = f"ide1_{token}b"
    prove_dir = RESULTS_ROOT / SYMBOL / f"ide1_{token}_prove"
    dir_a = RESULTS_ROOT / SYMBOL / key_a
    dir_b = RESULTS_ROOT / SYMBOL / key_b
    prove_dir.mkdir(parents=True, exist_ok=True)

    print(f"[prove-independent] run A {key_a}", flush=True)
    spawn_a = _spawn_e2e(key_a, dir_a)
    print(f"[prove-independent] run B {key_b}", flush=True)
    spawn_b = _spawn_e2e(key_b, dir_b)

    man_a = _load_manifest(dir_a)
    man_b = _load_manifest(dir_b)
    oracle_a = audit_directory(dir_a)
    oracle_b = audit_directory(dir_b)
    ab = compare_tables(dir_a, dir_b)

    pids_after = _pids()
    frozen_after = _frozen_hashes()
    gates = {
        "fresh_processes": 2 if man_a.get("pid") and man_b.get("pid") and man_a.get("pid") != man_b.get("pid") else 0,
        "run_a_ok": man_a.get("ok"),
        "run_b_ok": man_b.get("ok"),
        "golden_mismatches_run_a": int(((man_a.get("golden") or {}).get("n_mismatch")) or 0),
        "golden_mismatches_run_b": int(((man_b.get("golden") or {}).get("n_mismatch")) or 0),
        "run_a_vs_run_b_semantic_diffs": int(ab.get("semantic_diff_count") or 0),
        "content_hash_equal": bool(ab.get("content_hash_equal")),
        "oracle_false_positives": int(oracle_a.get("derived_false_positives") or 0) + int(oracle_b.get("derived_false_positives") or 0),
        "oracle_false_negatives": int(oracle_a.get("derived_false_negatives") or 0) + int(oracle_b.get("derived_false_negatives") or 0),
        "oracle_look_ahead_violations": int(oracle_a.get("look_ahead_violations") or 0) + int(oracle_b.get("look_ahead_violations") or 0),
        "raw_input_hashes_unchanged": bool(man_a.get("raw_input_hashes_unchanged")) and bool(man_b.get("raw_input_hashes_unchanged")),
        "used_config_first_touch_as_input": man_a.get("used_config_first_touch_as_input") or man_b.get("used_config_first_touch_as_input"),
        "used_config_detection_as_input": man_a.get("used_config_detection_as_input") or man_b.get("used_config_detection_as_input"),
        "independent_verdict_a": man_a.get("independent_derivation_verdict"),
        "independent_verdict_b": man_b.get("independent_derivation_verdict"),
        "live_pids_unchanged": _pids_unchanged(pids_before, pids_after),
    }
    verdict, reasons = decide_verdict(gates)

    payload = {
        "verdict": verdict,
        "reasons": reasons,
        "prove_dir": str(prove_dir),
        "created_at": _now(),
        "elapsed_s": round(time.monotonic() - t0, 3),
        "peak_ram_gb": _peak(),
        "git": _git(),
        "run_a": {
            "run_key": key_a,
            "dir": str(dir_a),
            "pid": man_a.get("pid"),
            "spawn": spawn_a,
            "manifest_summary": {
                "ok": man_a.get("ok"),
                "independent_derivation_verdict": man_a.get("independent_derivation_verdict"),
                "independent_timing": man_a.get("independent_timing"),
                "independent_wall_first_touch": man_a.get("independent_wall_first_touch"),
            },
        },
        "run_b": {
            "run_key": key_b,
            "dir": str(dir_b),
            "pid": man_b.get("pid"),
            "spawn": spawn_b,
            "manifest_summary": {
                "ok": man_b.get("ok"),
                "independent_derivation_verdict": man_b.get("independent_derivation_verdict"),
                "independent_timing": man_b.get("independent_timing"),
                "independent_wall_first_touch": man_b.get("independent_wall_first_touch"),
            },
        },
        "gates": gates,
        "oracle_a": oracle_a,
        "oracle_b": oracle_b,
        "ab": ab,
        "frozen_before": frozen_before,
        "frozen_after": frozen_after,
        "pids_before": pids_before,
        "pids_after": pids_after,
    }
    atomic_write_json(prove_dir / "prove_manifest.json", payload)
    atomic_write_json(prove_dir / "oracle_a.json", oracle_a)
    atomic_write_json(prove_dir / "oracle_b.json", oracle_b)
    atomic_write_json(prove_dir / "ab_compare.json", ab)
    atomic_write_text(
        prove_dir / "STATUS",
        "\n".join(
            [
                verdict,
                f"run_a={key_a}",
                f"run_b={key_b}",
                f"oracle_false_positives={gates['oracle_false_positives']}",
                f"oracle_false_negatives={gates['oracle_false_negatives']}",
                f"oracle_look_ahead_violations={gates['oracle_look_ahead_violations']}",
            ]
        )
        + "\n",
    )
    print(json.dumps({"verdict": verdict, "reasons": reasons, "prove_dir": str(prove_dir)}, indent=2), flush=True)
    return payload

"""E2E A/B for outcome contract."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.persist import atomic_write_json, atomic_write_text
from ..level_first_episode1_corrected_sms1_persist_v1.runner import _frozen_hashes, _pids
from ..paths import ENGINE_ROOT
from . import ALLOW_CLICKHOUSE_WRITES, VERDICT_OK
from .pipeline import run_outcome_contract

RESULTS_ROOT = ENGINE_ROOT / "results" / "wall_defense_outcome_contract_v1"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_e2e(*, run_key: str, out_dir: Path | None = None) -> dict[str, Any]:
    if ALLOW_CLICKHOUSE_WRITES:
        raise RuntimeError("CH writes forbidden")
    t0 = time.monotonic()
    pids_before = _pids()
    frozen_before = _frozen_hashes()
    out_dir = Path(out_dir or (RESULTS_ROOT / "BTCUSDT" / run_key))
    result = run_outcome_contract(run_key=run_key, out_dir=out_dir)
    pids_after = _pids()
    frozen_after = _frozen_hashes()
    manifest = {
        **result,
        "pid": os.getpid(),
        "fresh_process": True,
        "pids_before": pids_before,
        "pids_after": pids_after,
        "pids_unchanged": pids_before == pids_after,
        "frozen_hashes_unchanged": frozen_before == frozen_after,
        "clickhouse_writes": False,
        "commit": False,
        "push": False,
        "elapsed_s": time.monotonic() - t0,
        "created_at": _now(),
    }
    atomic_write_json(out_dir / "run_manifest.json", manifest)
    atomic_write_text(out_dir / "STATUS", manifest.get("verdict", "BLOCKED") + "\n")
    # Enrich Pflichtbericht with live-safety + run IDs
    pb_path = out_dir / "pflichtbericht.json"
    if pb_path.exists():
        pb = json.loads(pb_path.read_text(encoding="utf-8"))
        pb["run_key"] = run_key
        pb["live_safety"] = {
            "pids_unchanged": manifest.get("pids_unchanged"),
            "frozen_hashes_unchanged": manifest.get("frozen_hashes_unchanged"),
            "clickhouse_writes": False,
            "commit": False,
            "push": False,
            "pids_before": manifest.get("pids_before"),
            "pids_after": manifest.get("pids_after"),
        }
        atomic_write_json(pb_path, pb)
    return manifest


def compare_ab(dir_a: Path, dir_b: Path) -> dict[str, Any]:
    a = json.loads((Path(dir_a) / "run_manifest.json").read_text(encoding="utf-8"))
    b = json.loads((Path(dir_b) / "run_manifest.json").read_text(encoding="utf-8"))
    return {
        "ok": (
            a.get("ok")
            and b.get("ok")
            and a.get("semantic_hash") == b.get("semantic_hash")
            and a.get("outcome_contract_hash") == b.get("outcome_contract_hash")
            and a.get("verdict") == VERDICT_OK
        ),
        "semantic_hash_a": a.get("semantic_hash"),
        "semantic_hash_b": b.get("semantic_hash"),
        "outcome_contract_hash_a": a.get("outcome_contract_hash"),
        "outcome_contract_hash_b": b.get("outcome_contract_hash"),
        "verdict_a": a.get("verdict"),
        "verdict_b": b.get("verdict"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("e2e")
    p.add_argument("--run-key", required=True)
    p.add_argument("--out-dir", default=None)
    c = sub.add_parser("compare")
    c.add_argument("--dir-a", required=True)
    c.add_argument("--dir-b", required=True)
    c.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "e2e":
        man = run_e2e(run_key=args.run_key, out_dir=Path(args.out_dir) if args.out_dir else None)
        print(
            json.dumps(
                {
                    "verdict": man.get("verdict"),
                    "ok": man.get("ok"),
                    "semantic_hash": man.get("semantic_hash"),
                    "outcome_contract_hash": man.get("outcome_contract_hash"),
                    "oracle": man.get("oracle"),
                    "n_valid": man.get("n_valid"),
                    "n_censored": man.get("n_censored"),
                    "n_reclaim_labels_valid": man.get("n_reclaim_labels_valid"),
                },
                indent=2,
            )
        )
        return 0 if man.get("ok") else 2
    if args.cmd == "compare":
        rep = compare_ab(Path(args.dir_a), Path(args.dir_b))
        atomic_write_json(Path(args.out), rep)
        print(json.dumps(rep, indent=2))
        return 0 if rep["ok"] else 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

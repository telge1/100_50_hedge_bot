"""Two fresh processes: independent touch/detection derivation + oracle parity."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.persist import atomic_write_json, atomic_write_text
from ..drilldown.replay import clear_segment_cache, replay_window
from ..level_first_episode1_corrected_sms1_persist_v1.runner import _frozen_hashes, _pids
from ..market_profile_lld_shared_event_materialization_v1.hashing import sha256_hex
from ..paths import ENGINE_ROOT
from . import ALLOW_CLICKHOUSE_WRITES, RUN_PREFIX, SYMBOL, VERDICT_OK
from .derive import derive_all
from .oracle import audit_parity

RESULTS_ROOT = ENGINE_ROOT / "results" / "level_first_episode1_touch_detection_independent_v1"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _jsonify(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonify(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonify(v) for v in value]
    return value


def run_once(*, run_key: str, out_dir: Path, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    if ALLOW_CLICKHOUSE_WRITES:
        raise RuntimeError("CH writes forbidden")
    t0 = time.monotonic()
    pids_before = _pids()
    frozen_before = _frozen_hashes()
    clear_segment_cache()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # First pass without replay to get derived timing for evidence window
    pre = derive_all(cfg=cfg, replay=None, allow_archive_replay=False)
    from ..bounded_level_first_analyzer_pilot_v1.episodes import parse_utc

    evidence_start = parse_utc(pre["timing"]["evidence_start"])
    detection = parse_utc(pre["timing"]["detection"])
    replay = replay_window(
        symbol=SYMBOL,
        window_start=evidence_start,
        window_end=detection,
        reference_cutoffs=[],
        trades=[],
    )
    prod = derive_all(cfg=cfg, replay=replay, allow_archive_replay=False)
    oracle = audit_parity(production=prod, replay=replay)

    ok = prod["verdict"] == VERDICT_OK and oracle["ok"] and prod["coverage_error"] is None
    pids_after = _pids()
    frozen_after = _frozen_hashes()
    manifest = {
        "ok": ok,
        "verdict": prod["verdict"] if ok else ("ORACLE_MISMATCH" if not oracle["ok"] else prod["verdict"]),
        "run_key": run_key,
        "pid": os.getpid(),
        "fresh_process": True,
        "pids_before": pids_before,
        "pids_after": pids_after,
        "pids_unchanged": pids_before == pids_after,
        "frozen_hashes_unchanged": frozen_before == frozen_after,
        "clickhouse_writes": False,
        "commit": False,
        "push": False,
        "semantic_fingerprint": prod["semantic_fingerprint"],
        "reference_comparison": prod["reference_comparison"],
        "oracle": oracle,
        "timing": prod["timing"],
        "input_hashes": prod["input_hashes"],
        "stripped_forbidden_config_keys": prod["stripped_forbidden_config_keys"],
        "used_first_touch_iso_as_input": False,
        "used_detection_iso_as_input": False,
        "read_existing_touch_artifacts_as_input": False,
        "elapsed_s": time.monotonic() - t0,
        "created_at": _now(),
    }
    atomic_write_json(out_dir / "derivation.json", _jsonify(prod))
    atomic_write_json(out_dir / "zone_first_touch.json", _jsonify(prod["zone_first_touch"]))
    atomic_write_json(out_dir / "wall_first_touch.json", _jsonify(prod["wall_first_touch"]))
    atomic_write_json(out_dir / "detection.json", _jsonify(prod["detection"]))
    atomic_write_json(out_dir / "oracle.json", _jsonify(oracle))
    atomic_write_json(out_dir / "run_manifest.json", _jsonify(manifest))
    atomic_write_text(out_dir / "STATUS", manifest["verdict"] + "\n")
    clear_segment_cache()
    return manifest


def compare_ab(dir_a: Path, dir_b: Path) -> dict[str, Any]:
    a = json.loads((Path(dir_a) / "run_manifest.json").read_text(encoding="utf-8"))
    b = json.loads((Path(dir_b) / "run_manifest.json").read_text(encoding="utf-8"))
    return {
        "ok": a.get("semantic_fingerprint") == b.get("semantic_fingerprint") and a.get("ok") and b.get("ok"),
        "semantic_fingerprint_a": a.get("semantic_fingerprint"),
        "semantic_fingerprint_b": b.get("semantic_fingerprint"),
        "verdict_a": a.get("verdict"),
        "verdict_b": b.get("verdict"),
        "timing_a": a.get("timing"),
        "timing_b": b.get("timing"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("e2e")
    p.add_argument("--run-key", required=True)
    p.add_argument("--out-dir", default=None)
    p.add_argument("--inject-forbidden-config", action="store_true", help="negative: inject FIRST_TOUCH_ISO into cfg")
    c = sub.add_parser("compare")
    c.add_argument("--dir-a", required=True)
    c.add_argument("--dir-b", required=True)
    c.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "e2e":
        cfg = {"FIRST_TOUCH_ISO": "1999-01-01T00:00:00Z", "first_touch": "1999-01-01T00:00:00Z"} if args.inject_forbidden_config else {}
        out = Path(args.out_dir) if args.out_dir else RESULTS_ROOT / "BTCUSDT" / args.run_key
        man = run_once(run_key=args.run_key, out_dir=out, cfg=cfg)
        print(json.dumps({"verdict": man["verdict"], "ok": man["ok"], "semantic_fingerprint": man["semantic_fingerprint"]}, indent=2))
        return 0 if man["ok"] else 2
    if args.cmd == "compare":
        rep = compare_ab(Path(args.dir_a), Path(args.dir_b))
        atomic_write_json(Path(args.out), rep)
        print(json.dumps(rep, indent=2))
        return 0 if rep["ok"] else 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

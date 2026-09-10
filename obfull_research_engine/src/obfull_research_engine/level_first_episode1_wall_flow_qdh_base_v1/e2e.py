"""Fresh E2E: independent derivation + OB persist + wall-flow/QDH (new run ids)."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.persist import atomic_write_json, atomic_write_text
from ..level_first_episode1_corrected_sms1_persist_v1 import e2e as base_e2e
from ..level_first_episode1_corrected_sms1_persist_v1.independent_derivation import INPUT_FREEZE_DIR
from ..level_first_episode1_corrected_sms1_persist_v1.runner import _frozen_hashes, _pids
from ..market_profile_lld_shared_event_materialization_v1.hashing import file_sha256, sha256_hex
from ..paths import ENGINE_ROOT
from . import ALLOW_CLICKHOUSE_WRITES, PROTECTED_PRIOR_RUNS, RUN_PREFIX, VERDICT_EVENT_TIME, VERDICT_LIVE_CAUSAL
from .independent_oracle import oracle_audit_bundle
from .pipeline import run_from_persist_dir, write_wall_flow_outputs, compute_wall_flow_bundle, _load_trades
from .wall_flow_attribution import wall_flow_event_to_row

RESULTS_ROOT = ENGINE_ROOT / "results" / "level_first_episode1_wall_flow_qdh_base_v1"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _assert_safe(run_key: str, out_dir: Path) -> None:
    if run_key in PROTECTED_PRIOR_RUNS or out_dir.name in PROTECTED_PRIOR_RUNS:
        raise RuntimeError(f"refusing to overwrite protected run: {run_key}")
    if any(out_dir.name.startswith(p) for p in ("e2e1_", "ide1_", "csp1_", "gop1_")) and out_dir.name in PROTECTED_PRIOR_RUNS:
        raise RuntimeError(f"refusing protected prefix collision: {out_dir}")


def run_wall_flow_e2e(*, run_key: str, out_dir: Path | None = None) -> dict[str, Any]:
    if ALLOW_CLICKHOUSE_WRITES:
        raise RuntimeError("CH writes forbidden")
    t0 = time.monotonic()
    pids_before = _pids()
    frozen_before = _frozen_hashes()

    out_dir = Path(out_dir or (RESULTS_ROOT / "BTCUSDT" / run_key))
    _assert_safe(run_key, out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1) Fresh base Episode-1 independent e2e into this directory (new run id)
    base = base_e2e.run_e2e(run_key=run_key, out_dir=out_dir)
    if not base.get("ok"):
        atomic_write_json(out_dir / "wall_flow_e2e_manifest.json", {"ok": False, "base": base, "created_at": _now()})
        return {"ok": False, "verdict": "BASE_E2E_FAILED", "run_key": run_key, "out_dir": str(out_dir), "base": base}

    # 2) Wall-flow / QDH on persisted readback + frozen trades (receive times)
    trades_path = INPUT_FREEZE_DIR / "public_trades_zone_window.jsonl"
    wf = run_from_persist_dir(
        persist_dir=out_dir,
        out_dir=out_dir,
        run_key=run_key,
        trades_path=trades_path,
    )

    # 3) Independent oracle
    exact = json.loads((out_dir / "wall_flow_events_exact.json").read_text(encoding="utf-8"))
    band = json.loads((out_dir / "wall_flow_events_band.json").read_text(encoding="utf-8"))
    # timeline from CSV is awkward; reload via summary + recompute anchors from feature file
    import csv

    timeline = []
    with (out_dir / "feature_timeline.csv").open(encoding="utf-8") as fh:
        timeline = list(csv.DictReader(fh))
    # coerce numerics lightly for oracle
    for row in timeline:
        for k, v in list(row.items()):
            if v == "" or v is None:
                row[k] = None
                continue
            if k in ("look_ahead",):
                row[k] = str(v).lower() in ("1", "true", "yes")
            else:
                try:
                    if "." in str(v) or "e" in str(v).lower():
                        row[k] = float(v)
                    else:
                        row[k] = int(v)
                except ValueError:
                    pass
        if "look_ahead" not in row or row["look_ahead"] is None:
            row["look_ahead"] = False

    raw_trades, _ = _load_trades(trades_path)
    summary = wf["content"]
    oracle = oracle_audit_bundle(
        production_exact_events=exact,
        production_band_events=band,
        production_timeline=timeline,
        raw_trades=raw_trades,
        symbol="BTCUSDT",
        production_dedup=summary["trade_dedup"],
        production_summary=summary,
    )
    atomic_write_json(out_dir / "wall_flow_oracle.json", oracle)

    golden_ok = int((base.get("golden") or {}).get("n_mismatch") or base.get("n_mismatch") or 0) == 0
    # base manifest has golden summary
    man = json.loads((out_dir / "run_manifest.json").read_text(encoding="utf-8"))
    n_mismatch = int((man.get("golden") or {}).get("n_mismatch") or man.get("n_golden_mismatch") or 0)
    n_states = int(man.get("n_states") or summary.get("n_states") or 0)
    golden_ok = n_mismatch == 0 and n_states == 4178

    live_ok = bool(summary.get("live_causal", {}).get("ok"))
    verdict = VERDICT_LIVE_CAUSAL if (oracle.get("ok") and golden_ok and live_ok) else (
        VERDICT_EVENT_TIME if (oracle.get("ok") and golden_ok) else "EPISODE1_WALL_FLOW_QDH_BASE_BLOCKED"
    )
    if not oracle.get("ok") or not golden_ok:
        verdict = "EPISODE1_WALL_FLOW_QDH_BASE_BLOCKED"

    pids_after = _pids()
    frozen_after = _frozen_hashes()
    manifest = {
        "ok": verdict in (VERDICT_EVENT_TIME, VERDICT_LIVE_CAUSAL),
        "verdict": verdict,
        "run_key": run_key,
        "out_dir": str(out_dir),
        "pid": os.getpid(),
        "pids_before": pids_before,
        "pids_after": pids_after,
        "pids_unchanged": pids_before == pids_after,
        "frozen_hashes_unchanged": frozen_before == frozen_after,
        "clickhouse_writes": False,
        "commit": False,
        "push": False,
        "base_e2e_ok": bool(base.get("ok")),
        "golden_n_mismatch": n_mismatch,
        "n_states": n_states,
        "oracle": oracle,
        "wall_flow_hashes": wf.get("hashes"),
        "semantic_fingerprint": wf["hashes"].get("semantic_fingerprint"),
        "input_trades_sha256": file_sha256(trades_path),
        "live_causal": summary.get("live_causal"),
        "WALL_STATE": summary.get("WALL_STATE"),
        "elapsed_s": time.monotonic() - t0,
        "created_at": _now(),
    }
    atomic_write_json(out_dir / "wall_flow_e2e_manifest.json", manifest)
    atomic_write_text(out_dir / "WALL_FLOW_STATUS", verdict + "\n")
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="episode1_wall_flow_qdh_base_v1")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_e2e = sub.add_parser("e2e")
    p_e2e.add_argument("--run-key", required=True)
    p_e2e.add_argument("--out-dir", default=None)
    p_from = sub.add_parser("from-persist")
    p_from.add_argument("--persist-dir", required=True)
    p_from.add_argument("--out-dir", required=True)
    p_from.add_argument("--run-key", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "e2e":
        out = Path(args.out_dir) if args.out_dir else None
        man = run_wall_flow_e2e(run_key=args.run_key, out_dir=out)
        print(json.dumps({"verdict": man.get("verdict"), "ok": man.get("ok"), "out_dir": man.get("out_dir")}, indent=2))
        return 0 if man.get("ok") else 2
    if args.cmd == "from-persist":
        r = run_from_persist_dir(
            persist_dir=Path(args.persist_dir),
            out_dir=Path(args.out_dir),
            run_key=args.run_key,
        )
        print(json.dumps({"ok": r["ok"], "verdict": r["content"]["verdict_candidate"], "out_dir": r["out_dir"]}, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

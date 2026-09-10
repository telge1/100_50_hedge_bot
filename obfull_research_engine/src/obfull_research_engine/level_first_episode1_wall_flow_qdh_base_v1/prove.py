"""Compare two fresh wall-flow E2E runs (content/semantic hashes)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.persist import atomic_write_json
from ..market_profile_lld_shared_event_materialization_v1.hashing import file_sha256


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _strip_non_semantic(obj: Any) -> Any:
    if isinstance(obj, list):
        return [_strip_non_semantic(x) for x in obj]
    if isinstance(obj, dict):
        return {
            k: _strip_non_semantic(v)
            for k, v in obj.items()
            if k not in ("computed_at", "processing_completed_at", "created_at", "elapsed_s", "pid")
        }
    return obj


def compare_runs(dir_a: Path, dir_b: Path) -> dict[str, Any]:
    dir_a, dir_b = Path(dir_a), Path(dir_b)
    man_a = _load(dir_a / "wall_flow_e2e_manifest.json")
    man_b = _load(dir_b / "wall_flow_e2e_manifest.json")
    sem_a = _load(dir_a / "semantic_fingerprint.json")
    sem_b = _load(dir_b / "semantic_fingerprint.json")
    files = [
        "wall_flow_summary.json",
        "wall_flow_events_exact.json",
        "wall_flow_events_band.json",
        "feature_timeline.csv",
        "semantic_fingerprint.json",
        "wall_flow_oracle.json",
    ]
    file_hashes = {}
    diffs = []
    semantic_event_mismatch = False
    for name in files:
        pa, pb = dir_a / name, dir_b / name
        ha = file_sha256(pa) if pa.is_file() else None
        hb = file_sha256(pb) if pb.is_file() else None
        equal = ha == hb and ha is not None
        # Event JSON may differ only in wall-clock computed_at.
        if name in ("wall_flow_events_exact.json", "wall_flow_events_band.json") and pa.is_file() and pb.is_file():
            sa = _strip_non_semantic(_load(pa) if name.endswith(".json") else None)
            # reload as json list
            import json as _json

            sa = _strip_non_semantic(_json.loads(pa.read_text(encoding="utf-8")))
            sb = _strip_non_semantic(_json.loads(pb.read_text(encoding="utf-8")))
            equal = sa == sb
            if not equal:
                semantic_event_mismatch = True
            file_hashes[name] = {
                "a": ha,
                "b": hb,
                "raw_equal": ha == hb,
                "semantic_equal": equal,
                "note": "computed_at excluded from semantic equality",
            }
        else:
            file_hashes[name] = {"a": ha, "b": hb, "equal": equal}
            if not equal:
                diffs.append(name)
    if semantic_event_mismatch:
        diffs.append("wall_flow_events_semantic")
    # golden from base run_manifest
    ga = _load(dir_a / "run_manifest.json")
    gb = _load(dir_b / "run_manifest.json")
    out = {
        "run_a": man_a.get("run_key"),
        "run_b": man_b.get("run_key"),
        "verdict_a": man_a.get("verdict"),
        "verdict_b": man_b.get("verdict"),
        "oracle_ok_a": (man_a.get("oracle") or {}).get("ok"),
        "oracle_ok_b": (man_b.get("oracle") or {}).get("ok"),
        "semantic_equal": sem_a == sem_b,
        "semantic_fingerprint_a": man_a.get("semantic_fingerprint"),
        "semantic_fingerprint_b": man_b.get("semantic_fingerprint"),
        "file_hashes": file_hashes,
        "file_diff_names": diffs,
        "golden_a": {"n_mismatch": (ga.get("golden") or {}).get("n_mismatch"), "n_states": ga.get("n_states")},
        "golden_b": {"n_mismatch": (gb.get("golden") or {}).get("n_mismatch"), "n_states": gb.get("n_states")},
        "ok": (
            not diffs
            and sem_a == sem_b
            and (ga.get("golden") or {}).get("n_mismatch") == 0
            and (gb.get("golden") or {}).get("n_mismatch") == 0
            and ga.get("n_states") == 4178
            and gb.get("n_states") == 4178
            and (man_a.get("oracle") or {}).get("ok")
            and (man_b.get("oracle") or {}).get("ok")
        ),
    }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir-a", required=True)
    ap.add_argument("--dir-b", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    report = compare_runs(Path(args.dir_a), Path(args.dir_b))
    atomic_write_json(Path(args.out), report)
    print(json.dumps({"ok": report["ok"], "file_diff_names": report["file_diff_names"]}, indent=2))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

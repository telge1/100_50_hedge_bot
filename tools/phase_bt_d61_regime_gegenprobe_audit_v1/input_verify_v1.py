"""Verify BT-D60 Gegenprobe inputs against BT-D60A evidence manifest."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
D60_EVIDENCE = ROOT / "tools/phase_bt_d60_ema_history_validation_v1/evidence_manifest.json"
D60_RESULTS = ROOT / "results/phase_bt_d60_ema_history_v1"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_inputs() -> dict[str, Any]:
    manifest = json.loads(D60_EVIDENCE.read_text(encoding="utf-8"))
    expected = manifest.get("local_result_artifacts_not_in_git", {}).get("sha256", {})
    files = {
        "regime_gegenprobe_asof.csv": D60_RESULTS / "regime_gegenprobe_asof.csv",
        "ema_history_manifest.json": D60_RESULTS / "ema_history_manifest.json",
        "convergence_by_probe.csv": D60_RESULTS / "convergence_by_probe.csv",
    }
    out: dict[str, Any] = {
        "bt60a_head": "9c794d7ee486638281eda7aa194bd81bf4e1dc35",
        "ema_definition": manifest.get("ema_definition_primary"),
        "d60_verdict": manifest.get("verdict"),
        "files": {},
        "all_ok": True,
    }
    for name, path in files.items():
        row: dict[str, Any] = {"path": str(path.relative_to(ROOT)), "exists": path.is_file()}
        if not path.is_file():
            row["ok"] = False
            out["all_ok"] = False
        else:
            actual = _sha256(path)
            exp = expected.get(name)
            row["sha256"] = actual
            row["expected_sha256"] = exp
            row["ok"] = exp == actual if exp else True
            if exp and exp != actual:
                out["all_ok"] = False
        out["files"][name] = row
    return out

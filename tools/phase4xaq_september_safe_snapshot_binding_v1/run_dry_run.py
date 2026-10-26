#!/usr/bin/env python3
"""Read-only 4X-AQ dry-run: binding preflight + tests (no execute/resume/publish)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results/phase4xaq_september_safe_snapshot_binding_v1"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    steps = []
    r1 = subprocess.run(
        [sys.executable, str(ROOT / "tools/phase4xap_september_immutable_extension_v1/run_phase4xap.py"), "--preflight-only"],
        capture_output=True,
        text=True,
    )
    steps.append({"step": "4xap_ch_preflight", "exit": r1.returncode, "stdout": r1.stdout[-500:]})
    r2 = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "test_binding.py")],
        capture_output=True,
        text=True,
    )
    steps.append({"step": "binding_tests", "exit": r2.returncode, "stdout": r2.stdout[-2000:]})
    summary = {
        "dry_run": True,
        "steps": steps,
        "ok": r1.returncode == 0 and r2.returncode == 0,
    }
    if (OUT / "test_results.json").is_file():
        summary["test_verdict"] = json.loads((OUT / "test_results.json").read_text()).get("verdict")
    (OUT / "dry_run.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

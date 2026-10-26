#!/usr/bin/env python3
"""BT-D4 enforced research integration run (density only, no trades)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parents[1]
sys.path.insert(0, str(TOOLS))

from integration_verification_v1 import verify_sep25_golden  # noqa: E402
from research_backtester_v1 import run_research_session  # noqa: E402

OUT = ROOT / "results" / "phase_bt_d4_enforced_depth2_backtester_v1"
PARITY_AS_OF = "2026-09-25T11:00:00Z"


def main() -> int:
    session = run_research_session()
    out_dir = session.write_artifacts()

    report: dict = {
        "verdict": "BACKTESTER_DEPTH2_INTEGRATION_PASS",
        "artifacts_dir": str(out_dir),
        "gates": {},
    }

    snap = session.snapshots.get(PARITY_AS_OF)
    if not snap:
        report["verdict"] = "BACKTESTER_DEPTH2_INTEGRATION_BLOCKED"
        report["gates"]["sep25_snapshot"] = {"ok": False, "reason": "missing snapshot"}
    else:
        report["gates"]["sep25_golden"] = verify_sep25_golden(
            snap, session.config.golden_reference_path
        )
        if not report["gates"]["sep25_golden"]["ok"]:
            report["verdict"] = "BACKTESTER_DEPTH2_INTEGRATION_BLOCKED"

    # Causal: distinct snapshots in sequence
    seq = list(session.config.causal_sequence_closes)
    seq_hashes = [
        json.dumps(session.snapshots[c], sort_keys=True)
        for c in seq
        if c in session.snapshots
    ]
    report["gates"]["causal_sequence"] = {
        "ok": len(set(seq_hashes)) == len(seq_hashes) and len(seq_hashes) >= 2,
        "distinct": len(set(seq_hashes)),
    }
    if not report["gates"]["causal_sequence"]["ok"]:
        report["verdict"] = "BACKTESTER_DEPTH2_INTEGRATION_BLOCKED"

    extra_ok = True
    for close in session.config.additional_parity_closes:
        s = session.snapshots.get(close)
        if not s or s.get("total_band_count", 0) < 1:
            extra_ok = False
    report["gates"]["additional_parity_closes"] = {"ok": extra_ok}

    report["performance"] = {
        "timing_per_close_s": session.timing_s,
        "timing_total_s": session.run_manifest.get("timing_total_s"),
        "timing_mean_s": session.run_manifest.get("timing_mean_s"),
    }
    report["run_manifest_hash"] = session.run_manifest.get("run_manifest_hash")

    out_path = OUT / "integration_report.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"verdict": report["verdict"], "report": str(out_path)}, indent=2))
    return 0 if report["verdict"] == "BACKTESTER_DEPTH2_INTEGRATION_PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())

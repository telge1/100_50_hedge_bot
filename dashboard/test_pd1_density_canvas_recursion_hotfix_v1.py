"""Phase 6XT canvas flag recursion hotfix — executable harness."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
HARNESS = Path(
    "/home/telgenbuescher/projects/pools+ob+delta_bot/logs/historical_import/"
    "overnight_2026/phase6xt_density_canvas_recursion_hotfix_v1/recursion_harness.js"
)


def _read(rel: str) -> str:
    return (DASH / rel).read_text(encoding="utf-8")


def test_recursion_harness_executes_flag_readers():
    proc = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    body = json.loads(proc.stdout)
    assert body["recursion_thrown"] is False
    assert body["repeated_reads_ok"] is True
    assert body["agreement_flag_on"] is True
    assert body["agreement_flag_off"] is True
    by_case = {c["case"]: c for c in body["cases"]}
    assert by_case["A_flag_missing"]["density"] is False
    assert by_case["B_flag_zero"]["density"] is False
    assert by_case["C_flag_one"]["density"] is True


def test_no_cyclic_wiring_in_sources():
    density = _read("static/js/research/pd1_exact_origin_density_research_v1.js")
    host = _read("static/js/research/research_charts.js")
    assert "deps.exactOriginCanvasEnabled" not in density
    assert "exactOriginCanvasEnabled:" not in host
    assert "PD1ExactOriginResearch.isCanvasPathEnabled" not in host
    assert "phase6xt-density-continuous-history-v1" in density


def test_canvas_integration_still_passes():
    proc = subprocess.run(
        [
            "node",
            str(
                Path(
                    "/home/telgenbuescher/projects/pools+ob+delta_bot/logs/historical_import/"
                    "overnight_2026/phase6xt_density_canvas_integration_v1/canvas_integration_harness.js"
                )
            ),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout)
    assert body["canvasFlagDefaultOff"] is True

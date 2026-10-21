"""Phase 6XT canvas density integration (Option C) — deterministic harness."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
HARNESS = Path(
    "/home/telgenbuescher/projects/pools+ob+delta_bot/logs/historical_import/"
    "overnight_2026/phase6xt_density_canvas_integration_v1/canvas_integration_harness.js"
)


def _read(rel: str) -> str:
    return (DASH / rel).read_text(encoding="utf-8")


def test_canvas_harness_parity_with_poc():
    proc = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout)
    assert body["canvasFlagDefaultOff"] is True
    assert body["pipeline"]["coversGoto145_merged"] is True
    assert body["pipeline"]["coversGoto145_slice900"] is False
    assert body["pipeline"]["mergedSegments"] < body["fixture"]["rawSweepZones"]
    assert body["pipeline"]["mergedSegments"] > 100
    assert body["staticChecks"]["paneHasEodCanvas"]
    assert body["staticChecks"]["chartHasSetExactOriginDensity"]
    assert body["staticChecks"]["chartDrawInLayout"]


def test_canvas_path_no_hard_slice_in_active_branch():
    mod = _read("static/js/research/pd1_exact_origin_density_research_v1.js")
    assert "isCanvasPathEnabled" in mod
    assert "mergeExactOriginSegments" in mod or "canvasOn" in mod
    assert "phase6xt_eod_density_canvas_v1" in mod
    assert mod.count("zones.slice(0, 900)") == 0
    assert "applyZoneCap" in mod  # legacy DOM path when flag off


def test_research_charts_canvas_wiring():
    js = _read("static/js/research/research_charts.js")
    assert "exactOriginDensityCanvasEnabled" in js
    assert "EXACT_ORIGIN_CANVAS_FLAG_KEY" in js
    assert "eodCanvasOn" in js
    assert "PD1ExactOriginResearch.isCanvasPathEnabled" not in js


def test_adapter_merge_and_cull_exports():
    ad = _read("static/js/research/pd1_exact_density_trp_adapter.js")
    assert "mergeExactOriginSegments" in ad
    assert "cullZonesToViewport" in ad
    assert "EOD_CANVAS_MAX_SEGMENTS" in ad

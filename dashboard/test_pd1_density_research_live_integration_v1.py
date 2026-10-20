"""Phase 6XT: Exact Origin-Density integrated into Research Live Chart."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
ART = Path(
    "/home/telgenbuescher/projects/pools+ob+delta_bot/logs/historical_import/"
    "overnight_2026/phase6xt_density_research_live_integration_v1"
)


def _read(name: str) -> str:
    return (DASH / name).read_text(encoding="utf-8")


def test_research_template_density_checkbox_and_scripts():
    html = _read("templates/research_charts.html")
    assert 'id="researchIndExactOriginDensity"' in html
    assert "B – Exact Origin-Density" in html
    assert 'id="researchExactOriginDensityHint"' in html
    assert "pd1_exact_density_trp_adapter.js" in html
    assert "pd1_exact_origin_density_research_v1.js" in html
    assert "phase6xt-density-canvas-recursion-hotfix-v1" in html
    assert "/live-charts/viewer-poc" not in html


def test_research_charts_wiring():
    js = _read("static/js/research/research_charts.js")
    assert "function exactOriginDensityEnabled()" in js
    assert "PD1ExactOriginResearch.install" in js
    assert "PD1ExactOriginResearch.scheduleViewport" in js
    assert "exactOriginDensityEnabled()" in js
    assert "disableExactOrigin" in js


def test_helpers_exact_origin_namespace():
    h = _read("static/js/research/research_scanner_common_helpers.js")
    assert "PD1_EXACT_ORIGIN_DENSITY_V1" in h
    assert "stripExactOriginDensityOverlays" in h
    assert "keepExactOriginDensity" in h


def test_integration_module_mutual_exclusion():
    mod = _read("static/js/research/pd1_exact_origin_density_research_v1.js")
    assert "disableHistoricalLmv2" in mod
    assert "disableExactOrigin" in mod
    assert "stripHistoricalLmv2Overlays" in mod
    assert "mutual exclusion" in mod.lower() or "Historical LLD V2" in mod
    assert "168" in mod
    assert "/live-charts/viewer/api/window" in mod


def test_adapter_unchanged_contract():
    ad = _read("static/js/research/pd1_exact_density_trp_adapter.js")
    assert "poolsToExactOriginZones" in ad
    assert "computeExactBandsForSide" in ad


@pytest.mark.timeout(45)
def test_window_api_phase5s_regression():
    code = """
import json, os
os.environ["HISTORICAL_LMV2_BATCH_READER"]="1"
from pd1_landscape_viewer_v1.historical_window_v1 import build_landscape_window
d=build_landscape_window("2026-09-14T21:45:00Z","2026-09-15T05:45:00Z")
print(json.dumps({"n":len(d["prices"]),"pools":len(d.get("pools") or []),
"p5s":d["provenance"].get("phase5s_batch_reader_active")}))
"""
    env = {**dict(__import__("os").environ)}
    env["PYTHONPATH"] = ":".join(
        [
            str(DASH.parent),
            str(DASH),
            "/home/telgenbuescher/projects/pools+ob+delta_bot",
            "/home/telgenbuescher/projects/trading_research_platform",
        ]
    )
    env["HISTORICAL_LMV2_BATCH_READER"] = "1"
    proc = subprocess.run(
        [str(DASH.parent / ".venv/bin/python"), "-c", code],
        capture_output=True,
        text=True,
        timeout=40,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout.strip().splitlines()[-1])
    assert body["p5s"] is True
    assert body["n"] == 97
    ART.mkdir(parents=True, exist_ok=True)
    (ART / "regression_results.json").write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")

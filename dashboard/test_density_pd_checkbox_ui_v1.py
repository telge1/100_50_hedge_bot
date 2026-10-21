"""Phase 6XT: Exact Origin Density PD checkbox UI (chart-pane TF)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
ART = Path(
    "/home/telgenbuescher/projects/pools+ob+delta_bot/logs/historical_import/"
    "overnight_2026/phase6xt_density_pd_checkbox_ui_v1"
)


def _read(name: str) -> str:
    return (DASH / name).read_text(encoding="utf-8")


def test_template_pd_checkboxes_no_membership_dropdown():
    html = _read("templates/research_charts.html")
    assert 'name="researchExactOriginPoolDetail"' in html
    assert 'value="1"' in html
    assert 'value="5"' in html
    assert 'value="10"' in html
    assert "researchExactOriginPd1Timeframe" not in html
    assert "researchExactOriginPdWrap" in html
    assert 'name="researchHistoricalLmv2PoolDetail"' in html


def test_js_chart_pane_timeframe_and_pd_independence():
    js = _read("static/js/research/pd1_exact_origin_density_research_v1.js")
    assert "phase6xt-exact-origin-canvas-planned-hotfix-v1" in js
    assert '"5m", pool_detail: 5' in js or "pool_detail: 5" in js
    assert "paneChartTimeframe" in js
    assert "researchExactOriginPoolDetail" in js
    assert "researchExactOriginPd1Timeframe" not in js
    assert "researchHistoricalLmv2PoolDetail" not in js.replace(
        'querySelectorAll(\'input[name="researchExactOriginPoolDetail"]\')', ""
    )
    assert "RELEASED_VIEWER_SCOPES" in js
    assert "onPaneTimeframeChange" in js
    assert "activeScopesForPane" in js


def test_research_charts_tf_change_hooks_exact_origin():
    rc = _read("static/js/research/research_charts.js")
    assert "onPaneTimeframeChange" in rc


def _py_env():
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
    return env


def test_api_15m_and_5m_pd1_regression():
    code = """
import json, os
os.environ["HISTORICAL_LMV2_BATCH_READER"]="1"
from pd1_landscape_viewer_v1.historical_window_v1 import build_landscape_window
r15=build_landscape_window("2026-09-14T21:45:00Z","2026-09-15T05:45:00Z")
r5=build_landscape_window("2026-09-16T00:00:00Z","2026-09-17T23:55:00Z",timeframe="5m",pool_detail=1)
print(json.dumps({
  "n15": len(r15["prices"]),
  "line15": r15["provenance"]["pipeline_line_id"],
  "n5": len(r5["prices"]),
  "line5": r5["provenance"]["pipeline_line_id"],
}))
"""
    proc = subprocess.run(
        [str(DASH.parent / ".venv/bin/python"), "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        env=_py_env(),
    )
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout.strip().splitlines()[-1])
    assert body["n15"] == 97
    assert body["line15"].endswith("15mpd1_v1")
    assert body["n5"] == 576
    assert body["line5"].endswith("5mpd1_v1")
    ART.mkdir(parents=True, exist_ok=True)
    (ART / "api_regression.json").write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")

"""Phase 6XT — EOD density canvas stacking hotfix (above LWC chart)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
HARNESS = Path(
    "/home/telgenbuescher/projects/pools+ob+delta_bot/logs/historical_import/"
    "overnight_2026/phase6xt_density_canvas_stacking_hotfix_v1/stacking_harness.js"
)


def _read(rel: str) -> str:
    return (DASH / rel).read_text(encoding="utf-8")


def test_eod_canvas_after_chart_and_z_index():
    pane = _read("static/research_trp/pane.html")
    chart_pos = pane.index('id="chart"')
    eod_pos = pane.index('id="eod-density-overlay"')
    assert chart_pos < eod_pos, "eod-density-overlay must follow #chart in DOM"
    css = _read("static/research_trp/style.css")
    m = re.search(r"#eod-density-overlay\s*\{[^}]*z-index:\s*(\d+)", css, re.S)
    assert m and int(m.group(1)) >= 2
    chart_z = re.search(r"#chart\s*\{[^}]*z-index:\s*(\d+)", css, re.S)
    assert chart_z and int(m.group(1)) > int(chart_z.group(1))
    assert "pointer-events: none" in re.search(r"#eod-density-overlay\s*\{[^}]*\}", css, re.S).group(0)
    chart_js = _read("static/research_trp/chart.js")
    assert 'canvas.style.zIndex = "2"' in chart_js
    host = _read("static/js/research/research_charts.js")
    assert 'ASSET_V = "phase6xt-density-fixed-tile-integration-v1"' in host


def test_stacking_harness_dom_order():
    proc = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout)
    assert body["domOrderOk"] is True
    assert body["zIndexEod"] == 2
    assert body["zIndexChart"] == 1
    assert body["clipUsesPlotRect"] is True


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

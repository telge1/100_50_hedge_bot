"""MC-LOAD-P4G — viewport preserve (logical right margin + barSpacing)."""

from pathlib import Path

CHART = Path(__file__).resolve().parent / "static/research_trp/chart.js"
HTML = Path(__file__).resolve().parent / "templates/research_charts.html"
RC = Path(__file__).resolve().parent / "static/js/research/research_charts.js"


def test_p4g_asset_bump():
    assert "mc-load-p4i-v1" in HTML.read_text(encoding="utf-8")


def test_p4g_viewport_restore_helpers():
    c = CHART.read_text(encoding="utf-8")
    assert "restoreViewportAfterDataChange" in c
    assert "emitViewportMutation" in c
    assert "captureViewportForRestore" in c
    assert "readViewportState" in c


def test_p4g_host_viewport_mutation_diag():
    js = RC.read_text(encoding="utf-8")
    assert "on_viewport_mutation" in js
    assert "viewport_mutation" in js
    assert "capturePaneViewportSnapshot(pane)" in js
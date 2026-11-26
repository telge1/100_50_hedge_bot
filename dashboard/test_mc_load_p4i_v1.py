"""MC-LOAD-P4I — final toolbar-equivalent reset after pane load."""

from pathlib import Path

RC = Path(__file__).resolve().parent / "static/js/research/research_charts.js"
CHART = Path(__file__).resolve().parent / "static/research_trp/chart.js"
HTML = Path(__file__).resolve().parent / "templates/research_charts.html"


def test_p4i_asset_bump():
    assert "mc-load-p4i-v1" in HTML.read_text(encoding="utf-8")


def test_p4i_final_view_reset_hooks():
    js = RC.read_text(encoding="utf-8")
    assert "runFinalPaneViewResetLikeToolbar" in js
    assert "scheduleFinalPaneViewReset" in js
    assert "mc_load_final_view_reset" in js
    assert "mcLoadShouldFinalizePaneView" in js
    assert "toolbar_resetView" in js


def test_p4i_chart_is_view_manual():
    assert "isViewManual" in CHART.read_text(encoding="utf-8")
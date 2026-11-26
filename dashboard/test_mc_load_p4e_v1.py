"""MC-LOAD-P4E — regression hooks for Phase B range, EMA gate, merge guards."""

from pathlib import Path

DASH = Path(__file__).resolve().parent
RC = DASH / "static/js/research/research_charts.js"
CHART = DASH / "static/research_trp/chart.js"
HTML = DASH / "templates/research_charts.html"
SVC = DASH / "research_charts/service.py"


def test_p4e_asset_bump():
    html = HTML.read_text(encoding="utf-8")
    js = RC.read_text(encoding="utf-8")
    assert "mc-load-p4i-v1" in html
    assert "research-mc-load-p4i-v1" in html or "research-mc-pool-p4b-v1" in html
    assert 'MC_LOAD_P3B_CODE = "mc-load-p4i-v1"' in js


def test_p4e_phase_b_live_range_and_guards():
    js = RC.read_text(encoding="utf-8")
    assert "mcLoadHistoryPhaseBRange" in js
    assert "mergeLiveTailCandles" in js
    assert "capturePaneViewportSnapshot" in js
    assert "_historyPhaseBInflight" in js
    assert "paneEmaCatchUpRange" in js
    assert "afterHistoryPhaseB" in js
    assert "completed_ema_stale" in js
    assert "viewportSnapshot" in CHART.read_text(encoding="utf-8")
    assert "_range_end_is_live" in SVC.read_text(encoding="utf-8")
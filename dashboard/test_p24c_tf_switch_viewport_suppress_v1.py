"""DOGE-LIVE-P24C — Chart/Pool TF switch: viewport suppress during programmatic EO refresh."""

from __future__ import annotations

from pathlib import Path

DASH = Path(__file__).resolve().parent
RC = DASH / "static/js/research/research_charts.js"
EO = DASH / "static/js/research/pd1_exact_origin_density_research_v1.js"
HTML = DASH / "templates/research_charts.html"


def test_p24c_viewport_suppress_helpers_in_research_charts():
    js = RC.read_text(encoding="utf-8")
    assert "exactOriginViewportSuppressDepth" in js
    assert "beginExactOriginViewportSuppress" in js
    assert "endExactOriginViewportSuppress" in js
    assert "exactOriginRefreshAfterTfSwitch" in js
    assert "finalizeExactOriginChartTfSwitch" in js
    assert "beginExactOriginViewportSuppress();" in js
    assert "runExactOriginPoolTfSwitch" not in js


def test_p24c_go_to_uses_refcount_suppress():
    js = RC.read_text(encoding="utf-8")
    assert "beginExactOriginViewportSuppress();" in js
    idx = js.find("exact-origin go-to")
    assert idx > 0
    chunk = js[max(0, idx - 800) : idx + 200]
    assert "beginExactOriginViewportSuppress" in chunk
    assert "endExactOriginViewportSuppress" in chunk
    assert "_suppressExactOriginViewport = true" not in chunk


def test_p24c_chart_tf_change_arms_suppress_before_load():
    js = RC.read_text(encoding="utf-8")
    assert "const exactOriginTfSwitch = exactOriginDensityEnabled()" in js
    assert js.index("beginExactOriginViewportSuppress()") < js.index('sourceAction: "tf-change"')
    assert "finalizeExactOriginChartTfSwitch(pane, loadPromise)" in js


def test_p24c_pool_tf_switch_wrapper_in_exact_origin_js():
    js = EO.read_text(encoding="utf-8")
    assert "runExactOriginPoolTfSwitch" in js
    assert "beginExactOriginViewportSuppress" in js
    assert "refreshExactOriginAfterTfSwitch" in js
    assert "researchExactOriginPoolTimeframe" in js


def test_p24c_cache_bust_in_html():
    html = HTML.read_text(encoding="utf-8")
    assert "pd1-p24f-1h-4h-bounds-v1" in html

"""Phase 6XT — Research true fullscreen (native API on #researchChartDock)."""

from __future__ import annotations

from pathlib import Path

DASH = Path(__file__).resolve().parent
JS = DASH / "static/js/research/research_charts.js"
CSS = DASH / "static/css/research.css"
HTML = DASH / "templates/research_charts.html"


def test_fullscreen_wiring_in_research_charts():
    js = JS.read_text(encoding="utf-8")
    assert 'ASSET_V = "phase6xt-research-fullscreen-tools-v3"' in js
    assert "function toggleResearchTrueFullscreen" in js
    assert "function researchFullscreenRoot" in js
    assert 'return $("researchFullscreenHost")' in js
    assert "requestFullscreen" in js
    assert "syncResearchFullscreenUi" in js
    assert "fullscreenchange" in js
    assert 'const fsBtn = $("researchFullscreenBtn")' in js
    assert "function onResearchFullscreenUserClick" in js
    assert "enterNativeResearchFullscreenFromUserGesture" in js
    assert 'onResearchFullscreenUserClick();' in js[js.find('researchFullscreenBtn'): js.find('bindHeightDrag')]
    assert "togglePaneFullscreen(pid)" not in js


def test_fullscreen_tools_host_in_template():
    html = HTML.read_text(encoding="utf-8")
    host_start = html.find('id="researchFullscreenHost"')
    assert host_start >= 0
    dock_start = html.find('id="researchChartDock"', host_start)
    host_end = html.find("<!-- #researchFullscreenHost -->", dock_start)
    assert dock_start > host_start and host_end > dock_start
    host_block = html[host_start:host_end]
    assert 'id="researchSymbol"' in host_block
    assert 'id="trpEmaSettings"' in host_block
    assert 'id="researchGoTo"' in host_block
    assert 'id="researchIndExactOriginDensity"' in host_block
    assert 'id="researchExactOriginMinDepth"' in host_block
    assert 'id="researchFullscreenBtn"' in host_block


def test_fullscreen_css_and_template():
    css = CSS.read_text(encoding="utf-8")
    assert "#researchFullscreenHost:fullscreen" in css
    html = HTML.read_text(encoding="utf-8")
    assert "researchFullscreenBtn" in html
    assert "phase6xt-research-fullscreen-tools-v3" in html
    assert "research.css?v=phase6xt-research-fullscreen-tools-v3" in html


def test_min_depth_tests_still_reference_density_version():
    ad = (DASH / "templates/research_charts.html").read_text(encoding="utf-8")
    assert "phase6xt-density-min-depth-slider-v1" in ad

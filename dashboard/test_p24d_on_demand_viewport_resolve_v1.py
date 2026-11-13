"""DOGE-LIVE-P24D — On-demand pool TF uses chart visible viewport, not full candle span."""

from __future__ import annotations

from pathlib import Path

DASH = Path(__file__).resolve().parent
EO = DASH / "static/js/research/pd1_exact_origin_density_research_v1.js"
HTML = DASH / "templates/research_charts.html"
RC = DASH / "static/js/research/research_charts.js"


def test_p24d_on_demand_pool_tf_chart_visible_priority():
    js = EO.read_text(encoding="utf-8")
    assert "poolTfPrefersChartVisibleViewport" in js
    assert "resolveChartVisibleViewSec" in js
    assert "chart_visible_on_demand_pool_tf" in js
    idx = js.find("function resolveViewSec")
    chunk = js[idx : idx + 4500]
    assert "onDemandPoolTf && !crossTf" in chunk
    assert chunk.index("chart_visible_on_demand_pool_tf") < chunk.index("candleBoundsSec(pane)")
    assert "!onDemandPoolTf && deps.SC_HELPERS" in chunk


def test_p24d_supersede_pins_visible_range():
    js = EO.read_text(encoding="utf-8")
    assert "chart_visible_supersede_retry" in js
    assert "resolveChartVisibleViewSec(pane, \"chart_visible_supersede_retry\")" in js


def test_p24d_p24c_still_wired():
    rc = RC.read_text(encoding="utf-8")
    assert "beginExactOriginViewportSuppress" in rc
    assert "finalizeExactOriginChartTfSwitch" in rc
    eo = EO.read_text(encoding="utf-8")
    assert "runExactOriginPoolTfSwitch" in eo


def test_p24d_cache_bust_html():
    html = HTML.read_text(encoding="utf-8")
    assert "pd1-p24f-1h-4h-bounds-v1" in html


def test_p24d_served_static_contains_fix():
    """Disk == served path (dashboard serves static files directly)."""
    eo = EO.read_text(encoding="utf-8")
    assert "chart_visible_on_demand_pool_tf" in eo

"""BT-D30A: live regression — asset cache alignment + BT-D26 load path."""

from __future__ import annotations

from pathlib import Path

DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
ASSET = "phase6xt-bt-d30a-live-v1"


def _read(rel: str) -> str:
    return (DASH / rel).read_text(encoding="utf-8")


def test_html_and_js_asset_versions_aligned():
    html = _read("templates/research_charts.html")
    js = _read("static/js/research/research_charts.js")
    assert f'research_charts.js?v={ASSET}' in html
    assert f'ASSET_V = "{ASSET}"' in js
    assert "repaintExactOriginDensityAllVisiblePanes" in js
    assert "display_mode: btD26DisplayMode()" in js


def test_bt_d26_load_before_zoom_order():
    js = _read("static/js/research/research_charts.js")
    load_idx = js.index("async function applyBtD26DynamicSelectorBacktester")
    chunk = js[load_idx : load_idx + 2200]
    status_idx = chunk.index("setStatus((snap.backtester")
    zoom_idx = chunk.index("zoomChartToIsoRange")
    assert status_idx < zoom_idx, "success status must be set before optional zoom"


def test_workspace_accepts_display_mode():
    import inspect

    from research_charts.workspace_session import ResearchWorkspace

    sig = inspect.signature(ResearchWorkspace.load_bt_d26_dynamic_selector_catalog)
    assert "display_mode" in sig.parameters

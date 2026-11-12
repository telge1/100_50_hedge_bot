"""Contract tests — Research Chart long/short position order UI V1 (graphics only)."""

from __future__ import annotations

from pathlib import Path

DASH = Path(__file__).resolve().parent
TRP_JS = DASH / "static/research_trp/chart.js"
TRP_CSS = DASH / "static/research_trp/style.css"
PANE_HTML = DASH / "static/research_trp/pane.html"


def test_position_order_ui_markup_in_chart_js():
    js = TRP_JS.read_text(encoding="utf-8")
    assert "POSITION_ORDER_UI_ONLY" in js
    assert "pos-order-v1" in js
    assert "pos-order-controls" in js
    assert "pos-btn-setzen" in js
    assert "pos-btn-cancel" in js
    assert "pos-lamp-" in js
    assert "applyPositionLampUi" in js
    assert "POSITION_LAMP_LIVE_STATE" in js
    assert '"inactive"' in js or "'inactive'" in js
    assert "setPositionLampPreviewForTests" in js
    assert "initPositionOrderLegend" in js
    assert 'data-ui-only="1"' in js


def test_position_order_ui_styles_in_css():
    css = TRP_CSS.read_text(encoding="utf-8")
    assert ".pos-order-v1" in css
    assert ".pos-btn-setzen" in css
    assert ".pos-lamp-pending" in css
    assert ".pos-lamp-filled" in css
    assert ".pos-order-legend" in css
    assert ".pos-ctrl-mini" in css


def test_pane_cache_bust_for_position_ui():
    html = PANE_HTML.read_text(encoding="utf-8")
    assert "pos-order-ui-v1d" in html


def test_position_label_chrome_layout_helpers():
    js = TRP_JS.read_text(encoding="utf-8")
    assert "layoutPositionOrderChrome" in js
    assert "placePositionLabelNearPrice" in js
    assert "pos-ctrl-dock-outside" in js
    css = TRP_CSS.read_text(encoding="utf-8")
    assert "font-size: 13px" in css
    assert "font-size: 12px" in css


def test_zone_rendering_contract_unchanged():
    js = TRP_JS.read_text(encoding="utf-8")
    assert "renderPosition" in js
    assert "placeZone(profitEl" in js
    assert "positionHandlePoints" in js
    assert "ensurePositionDom" in js


def test_setzen_click_not_blocked_by_control_shell():
    js = TRP_JS.read_text(encoding="utf-8")
    assert "isPosOrderActionClick" in js
    assert "pos-btn-setzen" in js


def test_position_labels_use_mlb_display_notional():
    js = TRP_JS.read_text(encoding="utf-8")
    host = (DASH / "static/js/research/research_charts.js").read_text(encoding="utf-8")
    assert "resolvePositionDisplayNotional" in js
    assert "refreshMlbSizingConfig" in host
    assert "sizing-config" in host

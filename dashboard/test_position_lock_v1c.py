"""Position box edit lock (drawing.locked) — UI V1C contracts."""

from __future__ import annotations

from pathlib import Path

DASH = Path(__file__).resolve().parent
TRP_JS = DASH / "static/research_trp/chart.js"
TRP_CSS = DASH / "static/research_trp/style.css"
WS = DASH / "research_charts/workspace_session.py"


def test_lock_toggle_wiring_in_chart_js():
    js = TRP_JS.read_text(encoding="utf-8")
    assert "toggle_lock" in js
    assert "resize-right" in js
    assert "ov-handle-lock-hint" in js
    assert "pos-edit-locked" in js


def test_lock_persisted_via_workspace_session():
    ws = WS.read_text(encoding="utf-8")
    assert "def set_drawing_lock" in ws
    assert 'kind == "toggle_lock"' in ws
    assert "dc_replace(drawing, locked=bool(locked))" in ws
    assert "persist_drawings()" in ws


def test_lamp_icons_updated_in_css():
    css = TRP_CSS.read_text(encoding="utf-8")
    assert "pos-lamp-inactive .pos-lamp-icon" in css
    assert "pos-lamp-pending .pos-lamp-icon" in css
    assert "pos-lamp-filled .pos-lamp-icon" in css
    assert "Limit-Order gesetzt" not in css


def test_drawing_model_locked_field_still_used():
    models = Path("/home/telgenbuescher/projects/trading_research_platform/drawings/models.py")
    compose = Path("/home/telgenbuescher/projects/trading_research_platform/drawings/compose.py")
    assert "locked: bool" in models.read_text(encoding="utf-8")
    assert '"locked": bool(drawing.locked)' in compose.read_text(encoding="utf-8")

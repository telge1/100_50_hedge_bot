"""API wiring contract for manual long box order routes."""

from __future__ import annotations

from pathlib import Path

DASH = Path(__file__).resolve().parent


def test_api_routes_registered():
    api = (DASH / "research_charts/api.py").read_text(encoding="utf-8")
    assert "/api/research/manual-long-box/prepare" in api
    assert "/api/research/manual-long-box/confirm" in api
    assert "/api/research/manual-long-box/cancel/prepare" in api
    assert "/api/research/manual-long-box/status" in api
    assert "/api/research/manual-long-box/sizing-config" in api


def test_bridge_adapter_exists():
    mod = (DASH / "research_charts/manual_long_box_orders_v1.py").read_text(encoding="utf-8")
    assert "order_controller" in mod
    assert "prepare_place" in mod


def test_chart_order_ui_wired():
    js = (DASH / "static/research_trp/chart.js").read_text(encoding="utf-8")
    host = (DASH / "static/js/research/research_charts.js").read_text(encoding="utf-8")
    assert "on_position_order" in host
    assert "POSITION_ORDER_UI_ONLY = false" in js
    assert "setPositionOrderStatuses" in js

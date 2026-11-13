"""DOGE-LIVE-P24F — 1h/4h on-demand bounds wiring in Exact-Origin caps load."""

from __future__ import annotations

from pathlib import Path

DASH = Path(__file__).resolve().parent
EO = DASH / "static/js/research/pd1_exact_origin_density_research_v1.js"
HPS = DASH / "research_charts/historical_pool_state_v1.py"
HTML = DASH / "templates/research_charts.html"


def test_p24f_load_caps_fetches_1h_and_4h_bounds():
    js = EO.read_text(encoding="utf-8")
    assert "ON_DEMAND_BOUNDS_FETCH_POOL_TFS" in js
    assert '"1h"' in js
    assert "timeframe=" in js
    assert "encodeURIComponent(poolTf)" in js
    assert js.count("doge-pd1-on-demand-viewer-bounds") >= 1
    assert "_onDemand1hPd1ViewerBounds" in js
    assert "_onDemand4hPd1ViewerBounds" in js


def test_p24f_scope_caps_on_demand_patch_1h():
    js = EO.read_text(encoding="utf-8")
    assert "on_demand_" in js
    assert 'sc.timeframe === "1h"' in js
    assert 'sc.timeframe === "4h"' in js
    assert "on_demand_4h_pd1_patch" not in js
    assert "Number(sc.pool_detail) === 1" in js


def test_p24f_backend_on_demand_1h_4h_pd1():
    py = HPS.read_text(encoding="utf-8")
    assert "on_demand_1hpd1_capabilities_scope" in py
    assert "on_demand_4hpd1_capabilities_scope" in py
    assert "on-demand viewer bounds support PD1 only" in py


def test_p24f_4h_hint_frozen_publish():
    js = EO.read_text(encoding="utf-8")
    assert "4h PD10" in js
    assert "Apr–Sep" in js


def test_p24f_cache_bust():
    html = HTML.read_text(encoding="utf-8")
    assert "pd1-p24f-1h-4h-bounds-v1" in html

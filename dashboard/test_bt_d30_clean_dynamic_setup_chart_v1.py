"""BT-D30: clean dynamic selector chart display (current candidate vs all setups)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
RUN = Path(
    "/home/telgenbuescher/projects/pools+ob+delta_bot/results/setup_scanner_runs/"
    "bt_d26_dynamic_referenz_sep11_24h_v1/setups.json"
)


def _read(rel: str) -> str:
    return (DASH / rel).read_text(encoding="utf-8")


def test_ui_wiring():
    html = _read("templates/research_charts.html")
    assert 'id="researchBtD26DisplayMode"' in html
    assert "current_candidate" in html
    assert 'id="researchBtD26CandidateDetail"' in html
    js = _read("static/js/research/research_charts.js")
    assert "btD26DisplayMode" in js
    assert "resolveBtD26AsOfUnix" in js
    assert "phase6xt-bt-d30a-live-v1" in js


def test_backtester_resolve_reference_times():
    from research_charts.bt_d26_dynamic_selector_backtester import (
        DISPLAY_MODE_CURRENT_CANDIDATE,
        build_clean_view_draw_specs,
        resolve_active_candidate_at_asof,
    )

    doc = json.loads(RUN.read_text(encoding="utf-8"))
    from datetime import datetime, timezone

    def ts(iso: str) -> int:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())

    r12 = resolve_active_candidate_at_asof(doc, ts("2026-09-11T12:00:00Z"))
    assert r12["status"] == "ACTIVE"
    assert r12["active"]["planned_entry"] == 0.08365

    r1535 = resolve_active_candidate_at_asof(doc, ts("2026-09-11T15:35:00Z"))
    assert r1535["status"] == "ACTIVE"
    assert abs(float(r1535["active"]["planned_entry"]) - 0.08681) < 1e-6

    before = resolve_active_candidate_at_asof(doc, ts("2026-09-11T11:55:00Z"))
    assert before["status"] == "NONE"

    clean = build_clean_view_draw_specs(
        doc,
        run_id="bt_d26_dynamic_referenz_sep11_24h_v1",
        config_hash=doc.get("config_hash"),
        view_start_unix=ts("2026-09-11T14:00:00Z"),
        view_end_unix=ts("2026-09-11T18:00:00Z"),
        as_of_unix=ts("2026-09-11T15:35:00Z"),
    )
    assert len(clean["position_specs"]) == 1
    assert clean["position_specs"][0]["entry"] == r1535["active"]["planned_entry"]
    assert len(clean["marker_specs"]) >= 1
    assert DISPLAY_MODE_CURRENT_CANDIDATE == "current_candidate"


def test_episode_analysis_counts():
    from research_charts.bt_d26_dynamic_selector_backtester import analyze_setup_episode_transitions

    doc = json.loads(RUN.read_text(encoding="utf-8"))
    rep = analyze_setup_episode_transitions(doc)
    assert rep["setup_count"] == 102
    assert rep["by_selection_reason"]["LOWER_ELIGIBLE_UPPER_POOL_CHANGED"] == 71
    assert rep["by_selection_reason"]["RESELECT_AFTER_INVALIDATION_BT_D21_LOWEST"] == 30


def test_d23_unchanged_contract():
    js = _read("static/js/research/research_charts.js")
    assert "syncBtD23ViewportRange" in js
    assert "filter_d23" not in js  # d23 logic stays server module name only
    py = _read("research_charts/bt_d23_setup_scanner_backtester.py")
    assert "pool_first_setup_scanner_bt_d23" in py

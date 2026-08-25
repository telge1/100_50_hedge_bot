"""Dashboard tests for EMA dual-cross multi-source backtester."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ema_backtester_import():
    from research_charts.ema_dual_cross_backtester import run_ema_dual_cross_backtest, STRATEGY_ID

    assert STRATEGY_ID == "ema_dual_cross_multisource_v1"
    assert callable(run_ema_dual_cross_backtest)


def test_api_supports_edc_strategy():
    text = (ROOT / "research_charts" / "api.py").read_text(encoding="utf-8")
    assert "ema_dual_cross_multisource_v1" in text
    assert "run_ema_dual_cross_backtest" in text


def test_legacy_cluster_sweep_unchanged():
    text = (ROOT / "research_charts" / "cluster_sweep_backtester.py").read_text(encoding="utf-8")
    assert "cluster_sweep_ema_9_20_59" in text


def test_stoch_unchanged():
    from research_charts.stoch_backtester import BACKTESTER_SOURCE

    assert BACKTESTER_SOURCE == "stoch_backtester"


def test_ui_strategy_option():
    html = (ROOT / "templates" / "research_charts.html").read_text(encoding="utf-8")
    assert "ema_dual_cross_multisource_v1" in html
    assert "LEGACY" in html


def test_ui_edc_panel_fields():
    js = (ROOT / "static" / "js" / "research" / "research_charts.js").read_text(encoding="utf-8")
    assert "ema_dual_cross" in js
    assert "final_verdict" in js
    assert "mfe_1h_pct" in js


def test_block_marker_no_ent_without_allow():
    from research_charts.ema_dual_cross_backtester import candidates_to_marker_specs

    cands = [
        {
            "candidate_id": "c1",
            "direction": "BULLISH",
            "candidate_at": "2026-08-01T12:00:00+00:00",
            "final_verdict": "BLOCK",
            "ema_after": {"close": 1.0},
        }
    ]
    marks = candidates_to_marker_specs(cands, kinds={"ENT", "ALLOW", "BLOCK"})
    kinds = {m["kind"] for m in marks}
    assert "ENT" not in kinds
    assert "BLOCK" in kinds


def test_edc_cfg_passthrough_wiring():
    bt = (ROOT / "research_charts" / "ema_dual_cross_backtester.py").read_text(encoding="utf-8")
    api = (ROOT / "research_charts" / "api.py").read_text(encoding="utf-8")
    js = (ROOT / "static" / "js" / "research" / "research_charts.js").read_text(encoding="utf-8")
    html = (ROOT / "templates" / "research_charts.html").read_text(encoding="utf-8")
    assert "build_ema_dual_cross_cfg" in bt
    assert "enable_compressed_rebound" in api
    assert "edcEnableSync" in html
    assert "edcEnableRebound" in html
    assert 'id="edcEnableRebound">' in html or 'id="edcEnableRebound" ' in html
    assert "enable_sync_cross" in js
    assert "30 * 24" in js
    assert "edcRangeHint" in html


def test_edc_rebound_default_off_in_ui():
    html = (ROOT / "templates" / "research_charts.html").read_text(encoding="utf-8")
    assert 'id="edcEnableRebound">' in html
    assert 'id="edcEnableRebound" checked' not in html


def test_history_go_to_wiring():
    html = (ROOT / "templates" / "research_charts.html").read_text(encoding="utf-8")
    js = (ROOT / "static" / "js" / "research" / "research_charts.js").read_text(encoding="utf-8")
    chart_js = (ROOT / "static" / "research_trp" / "chart.js").read_text(encoding="utf-8")
    assert "researchHistoryPreset" in html
    assert "researchGoToBtn" in html
    assert "researchSyncChartAfterBt" in html
    assert "history-6" in html
    assert "visiblePanesContainTime" in js
    assert "reloadVisibleHistory" in js
    assert "goToDateTime" in js
    assert "syncChartAfterBacktest" in js
    assert "history.pinned" in js
    assert "skipDefaultView" in js
    assert "focusOnTime" in chart_js
    assert "skipDefaultView" in chart_js
    assert 'reqBody.from' in js or "reqBody.from" in js


def test_edc_zoom_wiring():
    html = (ROOT / "templates" / "research_charts.html").read_text(encoding="utf-8")
    js = (ROOT / "static" / "js" / "research" / "research_charts.js").read_text(encoding="utf-8")
    assert "researchEdcZoom" in html
    assert "zoomToEdcCandidate" in js
    assert "setVisibleTimeRange" in js

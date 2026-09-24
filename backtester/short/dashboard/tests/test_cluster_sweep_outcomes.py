"""Dashboard tests for cluster-sweep 1h/4h outcomes."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_outcome_module_import():
    from research_charts.cluster_sweep_outcomes import enrich_backtest_with_outcomes

    assert callable(enrich_backtest_with_outcomes)


def test_backtester_wires_outcome_enrichment():
    text = (ROOT / "research_charts" / "cluster_sweep_backtester.py").read_text(encoding="utf-8")
    assert "enrich_backtest_with_outcomes" in text
    assert "outcome_analysis" in text


def test_ui_shows_outcome_fields():
    js = (ROOT / "static" / "js" / "research" / "research_charts.js").read_text(encoding="utf-8")
    assert "outcomes_1h_4h" in js
    assert "mfe_1h_pct" in js


def test_stoch_backtester_unchanged():
    from research_charts.stoch_backtester import BACKTESTER_SOURCE

    assert BACKTESTER_SOURCE == "stoch_backtester"

from __future__ import annotations

from pathlib import Path

import stoch_heavy_job_gate as heavy_gate

DASHBOARD = Path(__file__).resolve().parents[2]
JS = (DASHBOARD / "static" / "js" / "stoch_signale.js").read_text(encoding="utf-8")
HTML = (DASHBOARD / "templates" / "stoch_signale.html").read_text(encoding="utf-8")
CHART_JS = (DASHBOARD / "static" / "js" / "research" / "research_charts.js").read_text(encoding="utf-8")
API = (DASHBOARD / "research_charts" / "api.py").read_text(encoding="utf-8")
PKG = DASHBOARD.parent / "research" / "stoch_fade_evaluation"


def test_ui_eval_button_and_picker():
    assert "Frozen-NO_BE50-Outcomes berechnen" in HTML
    assert "stochFilterResearchEval" in HTML
    assert "Keine Evaluation (nur Plan)" in HTML
    assert "window.confirm" in JS
    assert "implicit_latest" in (DASHBOARD / "stoch_fade_research_evaluations" / "feed.py").read_text(
        encoding="utf-8"
    )
    assert "FROZEN_RESEARCH_EVALUATION" in JS
    assert "body.evaluation_id" in CHART_JS
    assert "FROZEN_NO_BE50_EVALUATED" in API
    assert "evaluate_signal_no_be50" in HTML


def test_engine_package_uses_no_be50_not_be50():
    text = ""
    for path in PKG.glob("*.py"):
        if path.name == "guards.py":
            continue
        text += path.read_text(encoding="utf-8")
    assert "evaluate_signal_no_be50" in text
    assert "from signal_generator.pipeline.outcome_eval import evaluate_signal_be50" not in text
    assert "OutcomeEvaluator(" not in text
    assert "from signal_generator.db.outcomes import" not in text


def test_heavy_gate_three_way_mapping():
    assert heavy_gate.ERR_EVAL_BLOCKS_FROZEN == "OUTCOME_EVAL_BLOCKS_FROZEN_RESEARCH"
    assert heavy_gate.ERR_FROZEN_BLOCKS_EVAL == "FROZEN_JOB_BLOCKS_OUTCOME_EVALUATION"
    assert heavy_gate.ERR_UPDATE_BLOCKS_EVAL == "UPDATE_JOB_BLOCKS_OUTCOME_EVALUATION"
    assert heavy_gate.ERR_EVAL_BLOCKS_UPDATE == "OUTCOME_EVAL_BLOCKS_CANDLE_UPDATE"
    existing = {"owner_type": heavy_gate.OWNER_FROZEN_OUTCOME_EVALUATION}
    assert (
        heavy_gate._conflict_error(existing, heavy_gate.OWNER_FROZEN_RESEARCH)
        == heavy_gate.ERR_EVAL_BLOCKS_FROZEN
    )
    existing_f = {"owner_type": heavy_gate.OWNER_FROZEN_RESEARCH}
    assert (
        heavy_gate._conflict_error(existing_f, heavy_gate.OWNER_FROZEN_OUTCOME_EVALUATION)
        == heavy_gate.ERR_FROZEN_BLOCKS_EVAL
    )

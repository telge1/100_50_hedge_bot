from __future__ import annotations

from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[2]
JS = (DASHBOARD / "static" / "js" / "stoch_signale.js").read_text(encoding="utf-8")
HTML = (DASHBOARD / "templates" / "stoch_signale.html").read_text(encoding="utf-8")
CHART_JS = (DASHBOARD / "static" / "js" / "research" / "research_charts.js").read_text(encoding="utf-8")
CHART_HTML = (DASHBOARD / "templates" / "research_charts.html").read_text(encoding="utf-8")
FEED = (DASHBOARD / "stoch_fade_research_jobs" / "feed.py").read_text(encoding="utf-8")
APP = (DASHBOARD / "app.py").read_text(encoding="utf-8")
API = (DASHBOARD / "research_charts" / "api.py").read_text(encoding="utf-8")
BT = (DASHBOARD / "research_charts" / "stoch_backtester.py").read_text(encoding="utf-8")
JOBS = (DASHBOARD / "stoch_fade_research_jobs" / "jobs.py").read_text(encoding="utf-8")


def test_baseline_filters_remain_in_markup():
    assert 'id="stochFilterStrategy"' in HTML
    assert "NO_BE50 (aktiv)" in HTML
    assert 'value="wave_fade_no_be50_v1" selected' in HTML
    assert 'id="stochFilterHours"' in HTML
    assert 'value="48" selected' in HTML


def test_job_source_hides_strategy_and_hours():
    assert "function applyJobSourceUi" in JS
    assert "Nicht anwendbar – Outcomes fehlen" in HTML
    assert "stochStrategyJobNote" in JS
    assert "stochJobWindowNote" in JS
    assert 'strat.hidden = on' in JS
    assert 'hours.hidden = on' in JS
    assert "Job-Fenster:" in JS
    assert "Exit-Policy nicht angewendet" in JS


def test_job_request_omits_strategy_and_hours():
    assert 'qs.set("hours", hours)' in JS
    assert "if (!jobId)" in JS
    job_block = JS.split("async function load(")[1].split("function wire(")[0]
    assert 'qs.set("strategy_version"' in job_block
    assert "if (!jobId)" in job_block
    hours_idx = job_block.index('qs.set("hours"')
    guard = job_block.rfind("if (!jobId)", 0, hours_idx)
    assert guard != -1
    strat_idx = job_block.index('qs.set("strategy_version"')
    strat_guard = job_block.rfind("if (!jobId)", 0, strat_idx)
    assert strat_guard != -1


def test_invalid_job_does_not_fallback_to_baseline():
    catalog = JS.split("async function loadResearchJobCatalog")[1].split("document.addEventListener")[0]
    assert "nicht im Katalog" in catalog
    assert "storedResearchJobId" in catalog
    assert 'sel.value = wanted' in catalog


def test_job_poll_skipped_baseline_poll_kept():
    assert "selectedResearchJobId()) return" in JS
    assert "refreshMs: 15000" in JS


def test_result_not_computed_chip():
    assert "nicht berechnet" in JS
    assert "outcomes_computed === false" in JS


def test_research_chart_job_body_omits_hours_and_no_be50():
    bt = CHART_JS.split("researchBacktesterBtn")[1].split("researchIndStoch")[0]
    assert 'body.hours = 48' in bt
    assert "if (jobId)" in bt
    assert 'body.source = "FROZEN_RESEARCH_JOB"' in bt
    assert "body.strategy_version = strategy" in bt
    job_idx = bt.index("if (jobId)")
    hours_idx = bt.index("body.hours = 48")
    assert hours_idx > job_idx
    assert "PLANNED_NO_OUTCOME" in bt
    assert "4h-Planhorizont nur visuelle Projektion" in bt
    assert "researchJobSourceNote" in CHART_HTML
    assert "kein NO_BE50" in CHART_JS


def test_feed_has_no_writer_or_outcome_engine():
    lower = FEED.lower()
    assert "clickhouse" not in lower
    assert "outcome_engine" not in lower
    assert "insert into" not in lower
    assert "publish" not in lower
    assert "from research.stoch_fade_runner" not in FEED


def test_create_job_post_unchanged_path():
    assert '@app.post("/api/stoch/frozen-fade-jobs")' in APP
    assert "handle_create_post" in JOBS
    assert "WORKER_SCRIPT" in JOBS


def test_backtester_job_mode_not_no_be50():
    assert 'bt["display_mode"] = "PLANNED_NO_OUTCOME"' in API
    assert 'bt["strategy_version"] = strategy_version or "wave_fade_no_be50_v1"' in API
    chunk = API.split("if source ==")[1].split("else:")[0]
    assert "wave_fade_no_be50_v1" not in chunk
    assert "PLANNED_NO_OUTCOME" in chunk
    assert "plan_only" in BT or "FROZEN_RESEARCH_JOB" in BT

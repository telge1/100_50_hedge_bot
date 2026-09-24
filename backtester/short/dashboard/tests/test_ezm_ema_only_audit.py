"""Audit: dashboard EZM job request persists ema_only computation_mode."""

from __future__ import annotations

import json
from pathlib import Path


def test_job_request_stores_computation_mode_ema_only(tmp_path, monkeypatch):
    from research_charts import ezm_jobs
    from stoch_fade_research_jobs.jobs import job_dir_for

    jobs = tmp_path / "jobs"
    monkeypatch.setenv("STOCH_FADE_RESEARCH_JOBS_ROOT", str(jobs))
    monkeypatch.setenv("STOCH_EZM_RUNNER_STUB", "success")
    monkeypatch.setattr(ezm_jobs, "known_symbols", lambda: {"DOGEUSDT"})
    monkeypatch.setattr(
        "stoch_fade_research_jobs.jobs.coverage_report",
        lambda **kwargs: {"coins": [{"symbol": "DOGEUSDT", "testable": True}]},
    )
    monkeypatch.setattr(
        "stoch_fade_research_jobs.jobs.filter_testable",
        lambda symbols, coins: (symbols, None),
    )
    monkeypatch.setattr("stoch_fade_research_jobs.jobs.active_job_id", lambda environ=None: None)
    monkeypatch.setattr(
        "stoch_fade_research_jobs.jobs._spawn_and_lock",
        lambda job_id, directory, environ=None, spawn=None: ({"success": True, "job_id": job_id}, 200),
    )
    monkeypatch.setattr(
        "stoch_heavy_job_gate.try_acquire",
        lambda owner, job_id, environ=None: (True, None, None),
    )

    payload, code = ezm_jobs.start_ezm_research_job(
        symbol="DOGEUSDT",
        start="2026-01-01T00:00:00Z",
        end="2026-01-02T00:00:00Z",
        computation_mode="ema_only",
        environ={"STOCH_FADE_RESEARCH_JOBS_ROOT": str(jobs)},
    )
    assert code == 200
    assert payload["computation_mode"] == "ema_only"
    job_dir = job_dir_for(payload["job_id"], {"STOCH_FADE_RESEARCH_JOBS_ROOT": str(jobs)})
    req = json.loads((job_dir / "request.json").read_text())
    manifest = json.loads((job_dir / "job_manifest.json").read_text())
    assert req["computation_mode"] == "ema_only"
    assert manifest["computation_mode"] == "ema_only"

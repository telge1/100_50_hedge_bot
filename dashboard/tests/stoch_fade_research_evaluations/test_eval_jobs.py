from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from stoch_fade_research_evaluations.jobs import handle_create_post, handle_resume_post, start_evaluation
from stoch_fade_research_evaluations.worker import run_evaluation
from stoch_fade_research_jobs.config import CAUSAL_MANIFEST_HASH, CONFIRMATION_POLICY, STRATEGY_VERSION
from stoch_universe_51.jsonio import write_json_atomic as atomic

NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)
ORIGIN = "https://dash.immotel.de"
JOB = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
RUN = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
START = "2026-08-01T00:00:00Z"
END = "2026-08-02T00:00:00Z"
DASHBOARD = Path(__file__).resolve().parents[2]


def _write_source_job(root: Path) -> None:
    job_dir = root / JOB
    run_dir = job_dir / "coin_runs" / "AAVEUSDT" / RUN
    run_dir.mkdir(parents=True)
    atomic(
        job_dir / "request.json",
        {
            "job_id": JOB,
            "fixed_strategy_version": STRATEGY_VERSION,
            "confirmation_policy": CONFIRMATION_POLICY,
            "causal_manifest_hash": CAUSAL_MANIFEST_HASH,
            "selected_symbols": ["AAVEUSDT"],
            "signal_start": START,
            "signal_end_exclusive": END,
        },
    )
    atomic(
        job_dir / "status.json",
        {
            "job_id": JOB,
            "state": "COMPLETED",
            "coins": [
                {
                    "symbol": "AAVEUSDT",
                    "state": "COMPLETED",
                    "runner_run_id": RUN,
                    "artifact_reference": f"coin_runs/AAVEUSDT/{RUN}",
                    "raw_total": 2,
                    "tier_a_total": 1,
                }
            ],
        },
    )
    atomic(job_dir / "combined_summary.json", {"raw_total": 2, "tier_a_total": 1})
    atomic(run_dir / "summary.json", {"run_id": RUN, "raw_total": 2, "tier_a_total": 1})
    atomic(
        run_dir / "run_manifest.json",
        {
            "run_id": RUN,
            "selected_symbol": "AAVEUSDT",
            "strategy_id": STRATEGY_VERSION,
            "confirmation_policy": CONFIRMATION_POLICY,
            "causal_manifest_hash": CAUSAL_MANIFEST_HASH,
            "signal_start": START,
            "signal_end_exclusive": END,
            "source_commit_pin": "f16ae32",
        },
    )
    (run_dir / "signals.jsonl").write_text(
        json.dumps(
            {
                "signal_id": "tier-1",
                "symbol": "AAVEUSDT",
                "tier_a": True,
                "strategy_version": STRATEGY_VERSION,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _env(tmp_path, monkeypatch):
    jobs = tmp_path / "fade_jobs"
    evals = tmp_path / "evals"
    jobs.mkdir()
    evals.mkdir()
    _write_source_job(jobs)
    heavy = tmp_path / "heavy.lock"
    gate = tmp_path / "start.lock"
    monkeypatch.setenv("STOCH_FADE_RESEARCH_JOBS_ROOT", str(jobs))
    monkeypatch.setenv("STOCH_FADE_RESEARCH_EVALUATIONS_ROOT", str(evals))
    monkeypatch.setenv("STOCH_HEAVY_JOB_GATE", str(heavy))
    monkeypatch.setenv("STOCH_DASHBOARD_START_GATE", str(gate))
    monkeypatch.setenv("STOCH_FADE_EVAL_STUB", "1")
    monkeypatch.setattr(
        "stoch_fade_research_evaluations.jobs.worker_is_live",
        lambda pid, evaluation_id: bool(pid),
    )
    monkeypatch.setattr(
        "stoch_heavy_job_gate.worker_is_live",
        lambda pid, job_id, owner_type: bool(pid),
    )
    return {
        "STOCH_FADE_RESEARCH_JOBS_ROOT": str(jobs),
        "STOCH_FADE_RESEARCH_EVALUATIONS_ROOT": str(evals),
        "STOCH_HEAVY_JOB_GATE": str(heavy),
        "STOCH_DASHBOARD_START_GATE": str(gate),
        "STOCH_FADE_EVAL_STUB": "1",
    }, jobs, evals


def test_auth_routes_require_auth():
    text = (DASHBOARD / "app.py").read_text(encoding="utf-8")
    assert '@app.post("/api/stoch/frozen-fade-evaluations")' in text
    chunk = text.split('@app.post("/api/stoch/frozen-fade-evaluations")')[1].split(
        "@app.get(\"/api/stoch/profits\")"
    )[0]
    assert chunk.count("require_auth") >= 4


def test_unknown_fields_rejected(tmp_path, monkeypatch):
    env, _, _ = _env(tmp_path, monkeypatch)
    payload, code = handle_create_post(
        body={"source_job_id": JOB, "strategy_version": "wave_fade_no_be50_v1"},
        origin=ORIGIN,
        referer=ORIGIN + "/stoch-signale",
        content_type="application/json",
        environ=env,
    )
    assert code == 400
    assert payload["error"] == "UNKNOWN_FIELDS"


def test_origin_required(tmp_path, monkeypatch):
    env, _, _ = _env(tmp_path, monkeypatch)
    payload, code = handle_create_post(
        body={"source_job_id": JOB},
        origin=None,
        referer=None,
        content_type="application/json",
        environ=env,
    )
    assert code == 403
    assert payload["error"] == "ORIGIN_FORBIDDEN"


def test_start_stub_and_resume_hash(tmp_path, monkeypatch):
    env, jobs, evals = _env(tmp_path, monkeypatch)

    def spawn(argv, cwd, log):
        return 4242

    payload, code = start_evaluation(JOB, environ=env, now=NOW, spawn=spawn)
    assert code == 200
    eid = payload["evaluation_id"]
    run_evaluation(evals / eid)
    status = json.loads((evals / eid / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "COMPLETED"
    assert status["combined_summary"]["execution_dedup_applied"] is False
    assert (evals / eid / "coin_runs" / "AAVEUSDT" / "outcomes.jsonl").is_file()
    assert (evals / eid / "outcomes.jsonl").is_file()
    assert (evals / eid / "combined_summary.json").is_file()
    root = json.loads((evals / eid / "combined_summary.json").read_text(encoding="utf-8"))
    alias = json.loads((evals / eid / "summary.json").read_text(encoding="utf-8"))
    assert root == alias
    assert root["exit_policy"] == "NO_BE50"
    coin = status["coins"][0]
    assert coin["source_raw_total"] == 2
    assert coin["source_tier_a_total"] == 1
    assert coin["raw_total"] == 2
    assert coin["completed_outcomes"] == 1
    manifest = json.loads((evals / eid / "evaluation_manifest.json").read_text(encoding="utf-8"))
    assert manifest["evaluation_data_end"] == "2026-08-11T08:01:00Z"
    assert manifest["side_effect_flags"]["writes_to_clickhouse"] is False

    payload2, code2 = handle_resume_post(
        evaluation_id=eid,
        origin=ORIGIN,
        referer=ORIGIN + "/stoch-signale",
        content_type="application/json",
        body={},
        environ=env,
        spawn=spawn,
    )
    assert code2 == 409
    assert payload2["error"] == "RESUME_NOT_ALLOWED"

    status["state"] = "FAILED"
    atomic(evals / eid / "status.json", status)
    (jobs / JOB / "status.json").write_text(
        (jobs / JOB / "status.json").read_text(encoding="utf-8").replace('"tier_a_total": 1', '"tier_a_total": 9'),
        encoding="utf-8",
    )
    payload3, code3 = handle_resume_post(
        evaluation_id=eid,
        origin=ORIGIN,
        referer=ORIGIN + "/stoch-signale",
        content_type="application/json",
        body={},
        environ=env,
        spawn=spawn,
    )
    assert code3 == 409
    assert payload3["error"] in ("SOURCE_HASH_MISMATCH", "SOURCE_JOB_NOT_SELECTABLE", "NO_TIER_A_SIGNALS")

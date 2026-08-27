"""EZM wiring into frozen-fade research jobs (minimal dispatch)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from stoch_fade_research_jobs.config import EZM_STRATEGY_ID, STRATEGY_VERSION
from stoch_fade_research_jobs.ezm_adapter import (
    candidate_is_chart_marker,
    candidate_to_signal_row,
    clamp_window,
)
from stoch_fade_research_jobs.feed import catalog_entry, load_job_signals, map_job_signal
from stoch_fade_research_jobs.jobs import handle_create_post
from stoch_fade_research_jobs.strategy_resolve import resolve_strategy_id
from stoch_fade_research_jobs.worker import run_job
from stoch_universe_51.jsonio import write_json_atomic as atomic

NOW = datetime(2026, 8, 15, 7, 56, 30, tzinfo=timezone.utc)
START = "2025-12-11T00:00:00Z"
END = "2026-08-15T07:56:00Z"
ORIGIN = "https://dash.immotel.de"


def _uni(tmp_path, symbols):
    path = tmp_path / "universe_tradeable_51.json"
    path.write_text(
        json.dumps({"target_size": len(symbols), "symbols": symbols, "source": "test"}),
        encoding="utf-8",
    )
    return path


def _env(tmp_path, monkeypatch, symbols=None):
    symbols = symbols or ["ETHUSDT", "SOLUSDT"]
    uni = _uni(tmp_path, symbols)
    jobs = tmp_path / "fade_jobs"
    jobs.mkdir()
    monkeypatch.setenv("STOCH_UNIVERSE_51_PATH", str(uni))
    monkeypatch.setenv("STOCH_FADE_RESEARCH_JOBS_ROOT", str(jobs))
    monkeypatch.setenv("STOCH_UNIVERSE_51_JOBS_ROOT", str(tmp_path / "update_jobs"))
    monkeypatch.setenv("STOCH_DASHBOARD_START_GATE", str(tmp_path / "start.lock"))
    monkeypatch.setenv("STOCH_HEAVY_JOB_GATE", str(tmp_path / "heavy.lock"))
    monkeypatch.setenv("STOCH_EZM_RUNNER_STUB", "1")
    monkeypatch.setenv("STOCH_FADE_CODE_REVISION", "testrev")
    monkeypatch.setattr(
        "stoch_fade_research_jobs.jobs.worker_is_live",
        lambda pid, job_id: bool(pid),
    )
    monkeypatch.setattr(
        "stoch_heavy_job_gate.worker_is_live",
        lambda pid, job_id, owner_type: bool(pid),
    )
    return {
        "STOCH_UNIVERSE_51_PATH": str(uni),
        "STOCH_FADE_RESEARCH_JOBS_ROOT": str(jobs),
        "STOCH_UNIVERSE_51_JOBS_ROOT": str(tmp_path / "update_jobs"),
        "STOCH_DASHBOARD_START_GATE": str(tmp_path / "start.lock"),
        "STOCH_HEAVY_JOB_GATE": str(tmp_path / "heavy.lock"),
        "STOCH_EZM_RUNNER_STUB": "1",
        "STOCH_FADE_CODE_REVISION": "testrev",
    }, symbols, jobs


def _coverage(symbols):
    return {
        "coins": [{"symbol": s, "coverage_status": "FULL", "testable": True} for s in symbols]
    }


def test_resolve_defaults_to_frozen():
    assert resolve_strategy_id(None) == STRATEGY_VERSION
    assert resolve_strategy_id("") == STRATEGY_VERSION
    assert resolve_strategy_id(EZM_STRATEGY_ID) == EZM_STRATEGY_ID


def test_unknown_strategy_rejected(tmp_path, monkeypatch):
    env, symbols, _ = _env(tmp_path, monkeypatch)
    payload, code = handle_create_post(
        body={
            "symbols": ["ETHUSDT"],
            "signal_start": START,
            "signal_end_exclusive": END,
            "strategy_id": "not_a_real_strategy",
        },
        origin=ORIGIN,
        referer=ORIGIN + "/stoch-signale",
        content_type="application/json",
        environ=env,
        now=NOW,
        spawn=lambda a, c, l: 1,
        coverage_payload=_coverage(symbols),
        disk_free=10**12,
    )
    assert code == 400
    assert payload["error"] == "UNKNOWN_STRATEGY_ID"


def test_legacy_body_without_strategy_id_still_frozen(tmp_path, monkeypatch):
    env, symbols, jobs = _env(tmp_path, monkeypatch)
    payload, code = handle_create_post(
        body={"symbols": ["ETHUSDT"], "signal_start": START, "signal_end_exclusive": END},
        origin=ORIGIN,
        referer=ORIGIN + "/stoch-signale",
        content_type="application/json",
        environ=env,
        now=NOW,
        spawn=lambda a, c, l: 4242,
        coverage_payload=_coverage(symbols),
        disk_free=10**12,
    )
    assert code == 200
    req = json.loads((jobs / payload["job_id"] / "request.json").read_text(encoding="utf-8"))
    assert req["strategy_id"] == STRATEGY_VERSION
    assert req["runner_kind"] == "stoch_fade_runner"


def test_ezm_job_create_and_worker_stub(tmp_path, monkeypatch):
    env, symbols, jobs = _env(tmp_path, monkeypatch)
    spawned = {}

    def spawn(argv, cwd, log):
        spawned["argv"] = argv
        return 5555

    payload, code = handle_create_post(
        body={
            "symbols": ["ETHUSDT", "SOLUSDT"],
            "signal_start": START,
            "signal_end_exclusive": END,
            "strategy_id": EZM_STRATEGY_ID,
        },
        origin=ORIGIN,
        referer=ORIGIN + "/stoch-signale",
        content_type="application/json",
        environ=env,
        now=NOW,
        spawn=spawn,
        coverage_payload=_coverage(symbols),
        disk_free=10**12,
    )
    assert code == 200, payload
    assert payload["strategy_id"] == EZM_STRATEGY_ID
    job_id = payload["job_id"]
    req = json.loads((jobs / job_id / "request.json").read_text(encoding="utf-8"))
    assert req["run_intent"] == "candidate_discovery"
    assert req["runner_kind"] == "ezm_continuous_discovery"
    assert req["symbols"] == ["ETHUSDT", "SOLUSDT"]
    assert "confirmation_policy" not in req or req.get("confirmation_policy") in (None, "")

    # Simulate worker (spawn stub only recorded argv; run worker in-process)
    monkeypatch.setenv("STOCH_EZM_RUNNER_STUB", "1")
    rc = run_job(jobs / job_id)
    assert rc == 0
    status = json.loads((jobs / job_id / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "COMPLETED"
    assert status["successful_coins"] == 2

    signals, scode = load_job_signals(job_id, environ=env)
    assert scode == 200
    rows = signals["rows"]
    assert len(rows) == 4  # 2 symbols × LONG+SHORT
    dirs = {r["direction"] for r in rows}
    assert dirs == {"LONG", "SHORT"}
    assert all(r["ezm_research"] for r in rows)
    assert all(r.get("tp_price") in (None, "") for r in rows)

    entry = catalog_entry(jobs / job_id)
    assert entry is not None
    assert entry["identity_kind"] == "EZM_CANDIDATE_DISCOVERY"


def test_ezm_marker_filter_skips_wait():
    wait = {
        "candidate_state": "wait_microstructure_confirmation",
        "candidate_direction": "NONE",
        "emit_directional_marker": False,
        "decision_at": START,
        "decision_price": 1.0,
        "symbol": "ETHUSDT",
        "episode_id": "e1",
    }
    assert candidate_is_chart_marker(wait) is False
    assert candidate_to_signal_row(wait, strategy_hash="x", job_start=START, job_end=END) is None
    # Stage-A leak: LONG + emit false must not become a chart marker
    leak = {
        "candidate_state": "block_flat_compression",
        "candidate_direction": "LONG",
        "emit_directional_marker": False,
        "decision_at": START,
        "decision_price": 1.0,
        "symbol": "ETHUSDT",
        "episode_id": "e1b",
    }
    assert candidate_is_chart_marker(leak) is False
    ok = {
        "candidate_state": "defense_rejection_confirmed",
        "candidate_direction": "LONG",
        "emit_directional_marker": True,
        "decision_at": START,
        "decision_price": 1.0,
        "symbol": "ETHUSDT",
        "episode_id": "e2",
    }
    assert candidate_is_chart_marker(ok) is True
    mapped = map_job_signal(
        candidate_to_signal_row(ok, strategy_hash="x", job_start=START, job_end=END),
        job_id="a" * 32,
        runner_run_id="r1",
    )
    assert mapped["direction"] == "LONG"
    assert mapped["entry_time"] == START


def test_clamp_window():
    cov = {
        "status": "OK",
        "discovery_start": "2026-01-01T00:00:00Z",
        "discovery_end": "2026-01-10T00:00:00Z",
    }
    start, end, err = clamp_window(
        cov,
        signal_start="2026-01-02T00:00:00Z",
        signal_end_exclusive="2026-01-05T00:00:00Z",
    )
    assert err is None
    assert start.isoformat().startswith("2026-01-02")
    assert end.isoformat().startswith("2026-01-05")
    start, end, err = clamp_window(
        cov,
        signal_start="2026-01-11T00:00:00Z",
        signal_end_exclusive="2026-01-12T00:00:00Z",
    )
    assert err == "EMPTY_CLAMPED_WINDOW"


def test_ezm_incomplete_stub(tmp_path, monkeypatch):
    env, symbols, jobs = _env(tmp_path, monkeypatch)
    monkeypatch.setenv("STOCH_EZM_RUNNER_STUB", "incomplete")
    env["STOCH_EZM_RUNNER_STUB"] = "incomplete"
    payload, code = handle_create_post(
        body={
            "symbols": ["ETHUSDT"],
            "signal_start": START,
            "signal_end_exclusive": END,
            "strategy_id": EZM_STRATEGY_ID,
        },
        origin=ORIGIN,
        referer=ORIGIN + "/stoch-signale",
        content_type="application/json",
        environ=env,
        now=NOW,
        spawn=lambda a, c, l: 1,
        coverage_payload=_coverage(symbols),
        disk_free=10**12,
    )
    assert code == 200
    rc = run_job(jobs / payload["job_id"])
    assert rc != 0
    status = json.loads((jobs / payload["job_id"] / "status.json").read_text(encoding="utf-8"))
    assert status["coins"][0]["state"] == "DATA_INCOMPLETE"
    assert status["state"] == "COMPLETED_WITH_ERRORS"


def test_ui_contains_ezm_option():
    html = (Path(__file__).resolve().parents[2] / "templates" / "stoch_signale.html").read_text(
        encoding="utf-8"
    )
    assert "ema_zone_microstructure_confirmation_v1" in html
    assert "EMA Zone Microstructure Confirmation V1" in html
    js = (Path(__file__).resolve().parents[2] / "static" / "js" / "stoch_signale.js").read_text(
        encoding="utf-8"
    )
    assert "researchJobStrategyId" in js
    assert "strategy_id" in js


def test_oa_clickhouse_env_overrides_and_restores(monkeypatch):
    import os

    from stoch_fade_research_jobs.ezm_adapter import oa_clickhouse_env

    monkeypatch.setenv("CLICKHOUSE_USER", "fade_gold_reader")
    monkeypatch.setenv("CLICKHOUSE_DATABASE", "signal_generator")
    monkeypatch.setenv("CLICKHOUSE_HOST", "127.0.0.1")
    monkeypatch.setenv("CLICKHOUSE_PORT", "8123")
    with oa_clickhouse_env():
        assert os.environ["CLICKHOUSE_USER"] != "fade_gold_reader"
        assert os.environ["CLICKHOUSE_DATABASE"] == "orderbook_analysis"
        assert os.environ.get("CLICKHOUSE_HTTP_PORT")
    assert os.environ["CLICKHOUSE_USER"] == "fade_gold_reader"
    assert os.environ["CLICKHOUSE_DATABASE"] == "signal_generator"

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from stoch_fade_research_jobs.jobs import (
    handle_create_post,
    handle_resume_post,
    current_or_last_status,
    reconcile_lock,
    job_dir_for,
    lock_path,
)
from stoch_fade_research_jobs.worker import run_job, runner_argv
from stoch_universe_51.jsonio import write_json_atomic as atomic

NOW = datetime(2026, 8, 15, 7, 56, 30, tzinfo=timezone.utc)
START = "2025-12-11T00:00:00Z"
END = "2026-08-15T07:56:00Z"
ORIGIN = "https://dash.immotel.de"
DASHBOARD = Path(__file__).resolve().parents[2]


def _uni(tmp_path, symbols):
    path = tmp_path / "universe_tradeable_51.json"
    path.write_text(
        json.dumps({"target_size": len(symbols), "symbols": symbols, "source": "test"}),
        encoding="utf-8",
    )
    return path


def _env(tmp_path, monkeypatch, symbols=None):
    symbols = symbols or ["ETHUSDT", "SOLUSDT", "LITUSDT"]
    uni = _uni(tmp_path, symbols)
    jobs = tmp_path / "fade_jobs"
    jobs.mkdir()
    gate = tmp_path / "start.lock"
    heavy = tmp_path / "heavy.lock"
    monkeypatch.setenv("STOCH_UNIVERSE_51_PATH", str(uni))
    monkeypatch.setenv("STOCH_FADE_RESEARCH_JOBS_ROOT", str(jobs))
    monkeypatch.setenv("STOCH_UNIVERSE_51_JOBS_ROOT", str(tmp_path / "update_jobs"))
    monkeypatch.setenv("STOCH_DASHBOARD_START_GATE", str(gate))
    monkeypatch.setenv("STOCH_HEAVY_JOB_GATE", str(heavy))
    monkeypatch.setenv("STOCH_FADE_RUNNER_STUB", "1")
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
        "STOCH_DASHBOARD_START_GATE": str(gate),
        "STOCH_HEAVY_JOB_GATE": str(heavy),
        "STOCH_FADE_RUNNER_STUB": "1",
        "STOCH_FADE_CODE_REVISION": "testrev",
    }, symbols, jobs


def _coverage(symbols, extra=None):
    coins = [{"symbol": s, "coverage_status": "FULL", "testable": True} for s in symbols]
    if extra:
        coins.extend(extra)
    return {"coins": coins}


def _post(env, symbols, **kwargs):
    body = {
        "symbols": symbols,
        "signal_start": kwargs.pop("signal_start", START),
        "signal_end_exclusive": kwargs.pop("signal_end_exclusive", END),
    }
    extra = kwargs.pop("body_extra", None)
    if extra:
        body.update(extra)
    return handle_create_post(
        body=body,
        origin=kwargs.pop("origin", ORIGIN),
        referer=kwargs.pop("referer", ORIGIN + "/stoch-signale"),
        content_type=kwargs.pop("content_type", "application/json"),
        environ=env,
        now=NOW,
        spawn=kwargs.pop("spawn", lambda argv, cwd, log: 4242),
        coverage_payload=kwargs.pop("coverage_payload", _coverage(symbols)),
        disk_free=kwargs.pop("disk_free", 10 * 1024 * 1024 * 1024),
    )


def test_auth_routes_require_auth():
    text = (DASHBOARD / "app.py").read_text(encoding="utf-8")
    assert '@app.get("/api/stoch/frozen-fade-jobs")' in text
    assert '@app.post("/api/stoch/frozen-fade-jobs")' in text
    assert "api_stoch_frozen_fade_jobs_create" in text
    assert "Depends(require_auth)" in text
    chunk = text.split("@app.post(\"/api/stoch/frozen-fade-jobs\")")[1].split("@app.get(\"/api/stoch/profits\")")[0]
    assert chunk.count("require_auth") >= 4


def test_json_origin_required(tmp_path, monkeypatch):
    env, symbols, _ = _env(tmp_path, monkeypatch)
    payload, code = handle_create_post(
        body={"symbols": ["ETHUSDT"], "signal_start": START, "signal_end_exclusive": END},
        origin=None,
        referer=None,
        content_type="application/json",
        environ=env,
        now=NOW,
        coverage_payload=_coverage(symbols),
        disk_free=10**12,
        spawn=lambda a, c, l: 1,
    )
    assert code == 403
    assert payload["error"] == "ORIGIN_FORBIDDEN"
    payload, code = handle_create_post(
        body={"symbols": ["ETHUSDT"], "signal_start": START, "signal_end_exclusive": END},
        origin=ORIGIN,
        referer=ORIGIN,
        content_type="text/plain",
        environ=env,
        now=NOW,
        coverage_payload=_coverage(symbols),
        disk_free=10**12,
        spawn=lambda a, c, l: 1,
    )
    assert code == 403
    assert payload["error"] == "JSON_CONTENT_TYPE_REQUIRED"


def test_one_many_and_51_symbols(tmp_path, monkeypatch):
    fifty_one = [f"C{i:02d}USDT" for i in range(51)]
    env, _, _ = _env(tmp_path, monkeypatch, fifty_one)
    payload, code = _post(env, ["C00USDT"], coverage_payload=_coverage(fifty_one))
    assert code == 200
    payload, code = _post(env, ["C00USDT", "C07USDT"], coverage_payload=_coverage(fifty_one))
    assert code == 409
    import stoch_heavy_job_gate as heavy_gate

    heavy_gate.release(payload.get("job_id") or "x", environ=env)
    # first job still holds gate; release by reading remaining lock
    lock = heavy_gate.read_gate(env)
    if lock:
        heavy_gate.release(str(lock.get("job_id")), environ=env)
    monkeypatch.setattr("stoch_fade_research_jobs.jobs.active_job_id", lambda environ=None: None)
    monkeypatch.setattr("stoch_fade_research_jobs.jobs.update_job_active_id", lambda environ=None: None)
    payload, code = _post(env, ["C00USDT", "C07USDT"], coverage_payload=_coverage(fifty_one))
    assert code == 200
    assert payload["symbols"] == ["C00USDT", "C07USDT"]
    lock = heavy_gate.read_gate(env)
    if lock:
        heavy_gate.release(str(lock.get("job_id")), environ=env)
    payload, code = _post(env, fifty_one, coverage_payload=_coverage(fifty_one))
    assert code == 200
    assert payload["symbols"] == fifty_one


def test_symbol_rejections(tmp_path, monkeypatch):
    env, symbols, _ = _env(tmp_path, monkeypatch)
    monkeypatch.setattr("stoch_fade_research_jobs.jobs.active_job_id", lambda environ=None: None)
    cases = [
        (["ACEUSDT"], "UNKNOWN_SYMBOL"),
        (["ETHUSDT", "ETHUSDT"], "DUPLICATE_SYMBOLS"),
        ([], "EMPTY_SYMBOLS"),
        (["ALL"], "FULL_51_TOKEN_FORBIDDEN"),
        (["*"], "FULL_51_TOKEN_FORBIDDEN"),
    ]
    for raw, err in cases:
        payload, code = _post(env, raw, coverage_payload=_coverage(symbols))
        assert code == 400, err
        assert payload["error"] == err
    too = [f"C{i:02d}USDT" for i in range(52)]
    uni = _uni(tmp_path, too)
    env["STOCH_UNIVERSE_51_PATH"] = str(uni)
    monkeypatch.setenv("STOCH_UNIVERSE_51_PATH", str(uni))
    payload, code = _post(env, too, coverage_payload=_coverage(too))
    assert code == 400
    assert payload["error"] == "TOO_MANY_SYMBOLS"


def test_extra_fields_and_strategy_rejected(tmp_path, monkeypatch):
    env, symbols, _ = _env(tmp_path, monkeypatch)
    payload, code = _post(
        env,
        ["ETHUSDT"],
        body_extra={"strategy_version": "other", "cleanup": True},
    )
    assert code == 400
    assert payload["error"] == "UNKNOWN_FIELDS"


def test_time_validation(tmp_path, monkeypatch):
    env, symbols, _ = _env(tmp_path, monkeypatch)
    monkeypatch.setattr("stoch_fade_research_jobs.jobs.active_job_id", lambda environ=None: None)
    payload, code = _post(env, ["ETHUSDT"], signal_start="not-a-date")
    assert payload["error"] == "INVALID_DATETIME"
    payload, code = _post(env, ["ETHUSDT"], signal_start=END, signal_end_exclusive=START)
    assert payload["error"] == "START_NOT_BEFORE_END"
    payload, code = _post(env, ["ETHUSDT"], signal_end_exclusive="2026-08-15T07:57:00Z")
    assert payload["error"] == "END_IN_OPEN_OR_FUTURE_MINUTE"
    payload, code = _post(env, ["ETHUSDT"], signal_start="2025-12-11T00:00:01Z")
    assert payload["error"] == "MINUTE_ALIGNMENT_REQUIRED"


def test_disk_and_409(tmp_path, monkeypatch):
    env, symbols, _ = _env(tmp_path, monkeypatch)
    payload, code = _post(env, ["ETHUSDT"], disk_free=1)
    assert code == 400
    assert payload["error"] == "INSUFFICIENT_DISK"
    payload, code = _post(env, ["ETHUSDT"])
    assert code == 200
    payload, code = _post(env, ["SOLUSDT"])
    assert code == 409


def test_get_status_does_not_start(tmp_path, monkeypatch):
    env, _, jobs = _env(tmp_path, monkeypatch)
    before = list(jobs.iterdir())
    current_or_last_status(env, now=NOW)
    after = list(jobs.iterdir())
    assert before == after


def test_worker_order_argv_and_continue(tmp_path, monkeypatch):
    env, symbols, jobs = _env(tmp_path, monkeypatch)
    seen = []
    import stoch_fade_research_jobs.worker as worker

    orig = worker._run_coin_process

    def fake(argv, cwd, log, timeout_s):
        seen.append(list(argv))
        assert argv[1] == "-m"
        assert argv[2] == "research.stoch_fade_runner"
        assert "--clickhouse-readonly" in argv
        assert argv.count("--symbol") == 1
        assert "--cleanup-first" not in argv
        symbol = argv[argv.index("--symbol") + 1]
        if symbol == "ETHUSDT":
            return "FAILED", 7
        return orig(argv, cwd, log, timeout_s)

    monkeypatch.setattr(worker, "_run_coin_process", fake)
    payload, code = _post(env, ["SOLUSDT", "ETHUSDT"], spawn=lambda a, c, l: 1)
    assert code == 200
    directory = job_dir_for(payload["job_id"], env)
    run_job(directory)
    assert seen[0][seen[0].index("--symbol") + 1] == "ETHUSDT"
    assert seen[1][seen[1].index("--symbol") + 1] == "SOLUSDT"
    status = json.loads((directory / "status.json").read_text())
    assert status["state"] == "COMPLETED_WITH_ERRORS"
    by = {c["symbol"]: c for c in status["coins"]}
    assert by["ETHUSDT"]["state"] == "FAILED"
    assert by["SOLUSDT"]["state"] == "COMPLETED"
    assert by["SOLUSDT"]["raw_total"] == 10
    assert by["SOLUSDT"]["tier_a_total"] == 2
    assert by["SOLUSDT"]["warmup_complete"] is True
    assert by["SOLUSDT"]["warmup_complete_by_tf"] == {
        "15m": True,
        "30m": True,
        "1h": True,
        "4h": True,
    }
    assert by["SOLUSDT"]["artifact_reference"] == "coin_runs/SOLUSDT/stubsolusdt"
    assert not str(by["SOLUSDT"]["artifact_reference"]).startswith("/")
    summary = json.loads((directory / "combined_summary.json").read_text())
    assert summary["execution_dedup_applied"] is False
    assert summary["outcome_evaluation_enabled"] is False
    assert summary["writes_to_clickhouse"] is False
    assert "PASSWORD" not in json.dumps(status)
    man = json.loads((directory / "job_manifest.json").read_text())
    assert man["sequential"] is True
    assert man["max_parallelism"] == 1
    assert man["fixed_strategy_version"] == "wave_fade_frozen_f16ae32_causal_entry_v1"


def test_timeout_and_single_child(tmp_path, monkeypatch):
    env, symbols, _ = _env(tmp_path, monkeypatch)
    import stoch_fade_research_jobs.worker as worker

    orig = worker._run_coin_process
    active = {"n": 0, "max": 0}

    def wrapped(argv, cwd, log, timeout_s):
        active["n"] += 1
        active["max"] = max(active["max"], active["n"])
        try:
            symbol = argv[argv.index("--symbol") + 1]
            if symbol == "ETHUSDT":
                return "TIMEOUT", -9
            return orig(argv, cwd, log, timeout_s)
        finally:
            active["n"] -= 1

    monkeypatch.setattr(worker, "_run_coin_process", wrapped)
    payload, code = _post(env, ["ETHUSDT", "SOLUSDT"], spawn=lambda a, c, l: 1)
    run_job(job_dir_for(payload["job_id"], env))
    assert active["max"] == 1
    status = json.loads(job_dir_for(payload["job_id"], env).joinpath("status.json").read_text())
    by = {c["symbol"]: c for c in status["coins"]}
    assert by["ETHUSDT"]["state"] == "TIMEOUT"
    assert by["SOLUSDT"]["state"] == "COMPLETED"


def test_runner_argv_helper():
    argv = runner_argv(
        python="/opt/py",
        symbol="ETHUSDT",
        start=START,
        end=END,
        out_root="/tmp/out",
    )
    assert argv[0] == "/opt/py"
    assert "--clickhouse-readonly" in argv
    assert argv.count("--symbol") == 1


def test_orphan_lock_and_no_kill_foreign(tmp_path, monkeypatch):
    env, _, jobs = _env(tmp_path, monkeypatch)
    job_id = "deadjob"
    d = jobs / job_id
    d.mkdir()
    atomic(d / "status.json", {"job_id": job_id, "state": "RUNNING", "coins": []})
    atomic(lock_path(env), {"job_id": job_id, "pid": 1})
    monkeypatch.setattr("stoch_fade_research_jobs.jobs.pid_alive", lambda pid: True)
    monkeypatch.setattr("stoch_fade_research_jobs.jobs._proc_cmdline", lambda pid: "/usr/bin/sshd")
    monkeypatch.setattr(
        "stoch_fade_research_jobs.jobs.worker_is_live",
        lambda pid, job_id: False,
    )
    assert reconcile_lock(env) is None
    status = json.loads((d / "status.json").read_text())
    assert status["state"] == "INTERRUPTED"
    assert not lock_path(env).exists()


def test_orphan_running_coin_not_left_running(tmp_path, monkeypatch):
    env, _, jobs = _env(tmp_path, monkeypatch)
    job_id = "deadrun1deadrun1deadrun1deadrun1"
    d = jobs / job_id
    d.mkdir()
    coins = [
        {
            "symbol": "XRPUSDT",
            "state": "RUNNING",
            "warmup_complete": None,
            "warmup_complete_by_tf": {},
            "warmup_schema_error": None,
        },
        {
            "symbol": "SOLUSDT",
            "state": "PENDING",
            "warmup_complete": None,
            "warmup_complete_by_tf": {},
            "warmup_schema_error": None,
        },
    ]
    atomic(d / "status.json", {"job_id": job_id, "state": "RUNNING", "coins": coins, "worker_pid": 1})
    atomic(d / "progress.json", {"coins": coins})
    atomic(lock_path(env), {"job_id": job_id, "pid": 1})
    monkeypatch.setattr("stoch_fade_research_jobs.jobs.pid_alive", lambda pid: True)
    monkeypatch.setattr("stoch_fade_research_jobs.jobs._proc_cmdline", lambda pid: "/usr/bin/sshd")
    monkeypatch.setattr("stoch_fade_research_jobs.jobs.worker_is_live", lambda pid, job_id: False)
    assert reconcile_lock(env) is None
    status = json.loads((d / "status.json").read_text())
    progress = json.loads((d / "progress.json").read_text())
    assert status["state"] == "INTERRUPTED"
    assert status["worker_pid"] is None
    by = {c["symbol"]: c for c in status["coins"]}
    assert by["XRPUSDT"]["state"] == "INTERRUPTED"
    assert by["SOLUSDT"]["state"] == "PENDING"
    pby = {c["symbol"]: c for c in progress["coins"]}
    assert pby["XRPUSDT"]["state"] == "INTERRUPTED"
    assert pby["SOLUSDT"]["state"] == "PENDING"


def test_warmup_display_label_pending_not_artifact_error():
    from stoch_fade_research_jobs.jobs import public_coin, warmup_display_label

    pending = {
        "symbol": "SOLUSDT",
        "state": "PENDING",
        "warmup_complete": None,
        "warmup_complete_by_tf": {},
        "warmup_schema_error": None,
    }
    running = {**pending, "state": "RUNNING", "symbol": "XRPUSDT"}
    interrupted = {**pending, "state": "INTERRUPTED"}
    failed = {**pending, "state": "FAILED", "warmup_schema_error": "WARMUP_SCHEMA_MISSING"}
    complete_bad = {
        "symbol": "ETHUSDT",
        "state": "COMPLETED",
        "warmup_complete": True,
        "warmup_complete_by_tf": {},
        "warmup_schema_error": "WARMUP_SCHEMA_MISSING",
    }
    complete_ok = {
        "symbol": "ETHUSDT",
        "state": "COMPLETED",
        "warmup_complete": True,
        "warmup_complete_by_tf": {"15m": True, "30m": True, "1h": True, "4h": True},
        "warmup_schema_error": None,
    }
    assert warmup_display_label(pending) == "Noch nicht gestartet"
    assert warmup_display_label(running) == "Läuft"
    assert warmup_display_label(interrupted) == "Unterbrochen"
    assert warmup_display_label(failed) == "Artefaktfehler"
    assert warmup_display_label(complete_bad) == "Artefaktfehler"
    assert warmup_display_label(complete_ok) == "vollständig"
    assert public_coin(pending)["warmup_label"] == "Noch nicht gestartet"
    assert public_coin(running)["warmup_label"] == "Läuft"


def test_sg_python_preflight_fail_closed(tmp_path):
    from worker_env import PINNED_SG_PYTHON, sg_python_preflight

    missing = sg_python_preflight({"STOCH_FADE_SG_PYTHON": str(tmp_path / "no-such-python")})
    assert missing["ok"] is False
    assert missing["error_code"] == "MISSING_SG_PYTHON"
    shell = sg_python_preflight({"STOCH_FADE_SG_PYTHON": "python3"})
    assert shell["ok"] is False
    assert shell["error_code"] == "MISSING_SG_PYTHON"
    foreign = sg_python_preflight(
        {
            "STOCH_FADE_SG_PYTHON": (
                "/home/telgenbuescher/projects/Signal_Generator_Ralf/"
                "signal_generator_stoch_waves/.venv/bin/python"
            )
        }
    )
    assert foreign["ok"] is False
    assert foreign["error_code"] == "GOLD_VENV_IMPORT_ORIGIN_FAIL"
    if PINNED_SG_PYTHON.is_file():
        ok = sg_python_preflight({})
        assert ok["ok"] is True
        assert str(ok["python_path"]).endswith("/wave_fade_gold_f16ae32/.venv/bin/python")


def test_resume_skips_complete(tmp_path, monkeypatch):
    env, symbols, _ = _env(tmp_path, monkeypatch)
    payload, code = _post(env, ["ETHUSDT", "SOLUSDT"], spawn=lambda a, c, l: 1)
    directory = job_dir_for(payload["job_id"], env)
    import stoch_fade_research_jobs.worker as worker
    orig = worker._run_coin_process

    def fail_second(argv, cwd, log, timeout_s):
        if argv[argv.index("--symbol") + 1] == "SOLUSDT":
            return "FAILED", 1
        return orig(argv, cwd, log, timeout_s)

    monkeypatch.setattr(worker, "_run_coin_process", fail_second)
    run_job(directory)
    req_before = (directory / "request.json").read_text()
    monkeypatch.setattr("stoch_fade_research_jobs.jobs.active_job_id", lambda environ=None: None)
    monkeypatch.setattr("stoch_fade_research_jobs.jobs.update_job_active_id", lambda environ=None: None)
    calls = {"n": 0}

    def count_spawn(argv, cwd, log):
        calls["n"] += 1
        return 7

    payload, code = handle_resume_post(
        job_id=payload["job_id"],
        origin=ORIGIN,
        referer=ORIGIN,
        content_type="application/json",
        body=None,
        environ=env,
        spawn=count_spawn,
    )
    assert code == 200
    assert (directory / "request.json").read_text() == req_before
    monkeypatch.setattr(worker, "_run_coin_process", orig)
    run_job(directory)
    status = json.loads((directory / "status.json").read_text())
    by = {c["symbol"]: c for c in status["coins"]}
    assert by["ETHUSDT"]["state"] in ("SKIPPED_RESUME_COMPLETE", "COMPLETED")
    assert by["SOLUSDT"]["state"] == "COMPLETED"


def test_resume_not_completed_job(tmp_path, monkeypatch):
    env, symbols, _ = _env(tmp_path, monkeypatch)
    payload, code = _post(env, ["ETHUSDT"], spawn=lambda a, c, l: 1)
    directory = job_dir_for(payload["job_id"], env)
    run_job(directory)
    monkeypatch.setattr("stoch_fade_research_jobs.jobs.active_job_id", lambda environ=None: None)
    payload, code = handle_resume_post(
        job_id=payload["job_id"],
        origin=ORIGIN,
        referer=ORIGIN,
        content_type="application/json",
        body=None,
        environ=env,
        spawn=lambda a, c, l: 1,
    )
    assert code == 409
    assert payload["error"] == "RESUME_NOT_ALLOWED"


def test_ui_and_default_strategy_unchanged():
    html = (DASHBOARD / "templates" / "stoch_signale.html").read_text(encoding="utf-8")
    js = (DASHBOARD / "static" / "js" / "stoch_signale.js").read_text(encoding="utf-8")
    assert "Frozen Stochastic Fade – Signale berechnen" in html
    assert "Kausalen Backtest starten" in html
    assert "Kausaler Backtest Ergebnis" in html
    assert 'id="frozenFadeResultCard"' in html
    assert "Nur Signalerzeugung" in html
    assert 'option value="wave_fade_no_be50_v1" selected' in html
    assert "universe51SelectAll" in html
    assert "universe51UpdateSelected" in html
    assert "setDesiredState" in js
    assert "startUniverse51Update" in js
    assert "frozenFade.pollMs = 3000" in js or "pollMs: 3000" in js
    assert "Keine Execution-Dedup-Policy" in html
    assert "Keine Outcomes berechnet" in html
    assert "Keine ClickHouse-Signale geschrieben" in html
    assert "value || \"wave_fade_no_be50_v1\"" in js
    assert "localStorage" not in js.split("wireFrozenFade")[1]


def test_incomplete_not_testable(tmp_path, monkeypatch):
    env, symbols, _ = _env(tmp_path, monkeypatch)
    payload, code = _post(
        env,
        ["ETHUSDT"],
        coverage_payload={"coins": [{"symbol": "ETHUSDT", "coverage_status": "INCOMPLETE", "testable": False}]},
    )
    assert code == 400
    assert payload["error"] == "SYMBOL_NOT_TESTABLE"


def test_warmup_and_relative_artifact_from_runner_fields(tmp_path):
    from stoch_fade_research_jobs.complete import counts_from_run
    from stoch_universe_51.jsonio import write_json_atomic

    run = tmp_path / "coin_runs" / "AAVEUSDT" / "def3dc3960ab4afa97c59e2c2ac444c7"
    (run / "per_symbol").mkdir(parents=True)
    write_json_atomic(run / "summary.json", {"run_id": run.name, "raw_total": 28, "tier_a_total": 2})
    write_json_atomic(
        run / "per_symbol" / "AAVEUSDT.json",
        {
            "warmup_complete": True,
            "counts_by_timeframe": {
                "15m": {"raw_candidates": 15, "tier_a": 0},
                "30m": {"raw_candidates": 8, "tier_a": 1},
                "1h": {"raw_candidates": 4, "tier_a": 0},
                "4h": {"raw_candidates": 1, "tier_a": 1},
            },
            "first_valid_by_timeframe": {
                "15m": {"warmup_complete": True},
                "30m": {"warmup_complete": True},
                "1h": {"warmup_complete": True},
                "4h": {"warmup_complete": True},
            },
        },
    )
    counts = counts_from_run(run, "AAVEUSDT")
    assert counts["warmup_complete"] is True
    assert counts["warmup_complete_by_tf"] == {
        "15m": True,
        "30m": True,
        "1h": True,
        "4h": True,
    }
    assert counts["artifact_reference"] == "coin_runs/AAVEUSDT/def3dc3960ab4afa97c59e2c2ac444c7"
    assert "/" != counts["artifact_reference"][0] or counts["artifact_reference"].startswith("coin_runs/")
    assert "/home/" not in counts["artifact_reference"]


def test_update_job_blocks_frozen(tmp_path, monkeypatch):
    env, symbols, _ = _env(tmp_path, monkeypatch)
    from stoch_universe_51.update_jobs import start_update_job

    coins = [
        {
            "symbol": "ETHUSDT",
            "coverage_status": "FULL",
            "testable": True,
            "freshness_status": "UPDATE_AVAILABLE",
            "data_from": "2025-12-11T00:00:00Z",
            "data_to": "2026-08-10T23:59:00Z",
            "candle_count": 100,
            "update_from": "2026-08-11T00:00:00Z",
        }
    ]
    monkeypatch.setattr("stoch_universe_51.update_jobs.worker_is_live", lambda pid, job_id: True)
    payload, code = start_update_job(
        ["ETHUSDT"],
        environ=env,
        now=NOW,
        spawn=lambda a, c, l: 99,
        coverage_payload={"coins": coins},
    )
    assert code == 200
    blocked, bcode = _post(env, ["ETHUSDT"])
    assert bcode == 409
    assert blocked["error"] == "UPDATE_JOB_BLOCKS_FROZEN_RESEARCH"


def test_cross_lock_race_only_one_starts(tmp_path, monkeypatch):
    import threading

    env, symbols, _ = _env(tmp_path, monkeypatch)
    from stoch_universe_51.update_jobs import start_update_job

    coins = [
        {
            "symbol": "ETHUSDT",
            "coverage_status": "FULL",
            "testable": True,
            "freshness_status": "UPDATE_AVAILABLE",
            "data_from": "2025-12-11T00:00:00Z",
            "data_to": "2026-08-10T23:59:00Z",
            "candle_count": 100,
        }
    ]
    monkeypatch.setattr("stoch_universe_51.update_jobs.worker_is_live", lambda pid, job_id: True)
    results = []

    def freeze():
        results.append(("f", _post(env, ["ETHUSDT"])))

    def update():
        results.append(
            (
                "u",
                start_update_job(
                    ["ETHUSDT"],
                    environ=env,
                    now=NOW,
                    spawn=lambda a, c, l: 88,
                    coverage_payload={"coins": coins},
                ),
            )
        )

    t1 = threading.Thread(target=freeze)
    t2 = threading.Thread(target=update)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    codes = {kind: pair[1] for kind, pair in results}
    oks = [c for c in codes.values() if c == 200]
    nines = [c for c in codes.values() if c == 409]
    assert len(oks) == 1
    assert len(nines) == 1
    err = [pair[0]["error"] for _, pair in results if pair[1] == 409][0]
    assert err in (
        "UPDATE_JOB_BLOCKS_FROZEN_RESEARCH",
        "FROZEN_JOB_ALREADY_RUNNING",
        "FROZEN_JOB_BLOCKS_CANDLE_UPDATE",
        "HEAVY_JOB_RESOURCE_BUSY",
        "UPDATE_JOB_ALREADY_RUNNING",
    )


def test_warmup_schema_variants(tmp_path):
    from stoch_fade_research_jobs.complete import warmup_from_per_symbol

    all_true = {tf: {"warmup_complete": True} for tf in ("15m", "30m", "1h", "4h")}
    overall, by, err = warmup_from_per_symbol({"warmup_complete": True, "first_valid_by_timeframe": all_true})
    assert overall is True and err is None and by["15m"] is True
    mixed = dict(all_true)
    mixed["4h"] = {"warmup_complete": False}
    overall, by, err = warmup_from_per_symbol({"warmup_complete": True, "first_valid_by_timeframe": mixed})
    assert overall is False and err == "WARMUP_INCONSISTENT" and by["4h"] is False
    overall, by, err = warmup_from_per_symbol(
        {"warmup_complete": False, "first_valid_by_timeframe": {tf: {"warmup_complete": False} for tf in all_true}}
    )
    assert overall is False and err is None
    overall, by, err = warmup_from_per_symbol({"warmup_complete": True, "first_valid_by_timeframe": {"15m": {"warmup_complete": True}}})
    assert overall is None and err == "WARMUP_SCHEMA_INCOMPLETE"
    overall, by, err = warmup_from_per_symbol(
        {"warmup_complete": True, "first_valid_by_timeframe": {**all_true, "15m": {"warmup_complete": "yes"}}}
    )
    assert err == "WARMUP_SCHEMA_INVALID"
    overall, by, err = warmup_from_per_symbol({"warmup_complete": True})
    assert err == "WARMUP_SCHEMA_MISSING"


def test_api_redacts_old_absolute_artifact(tmp_path, monkeypatch):
    from stoch_fade_research_jobs.jobs import public_status

    status = {
        "job_id": "5e059f73d87348bd87083a56368c70d5",
        "state": "COMPLETED",
        "worker_pid": 1162285,
        "coins": [
            {
                "symbol": "AAVEUSDT",
                "runner_run_id": "def3dc3960ab4afa97c59e2c2ac444c7",
                "artifact_reference": "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/results/stoch_fade_research_jobs/5e059f73d87348bd87083a56368c70d5/coin_runs/AAVEUSDT/def3dc3960ab4afa97c59e2c2ac444c7",
                "warmup_complete": True,
                "warmup_complete_by_tf": {},
            }
        ],
    }
    pub = public_status(status)
    blob = json.dumps(pub)
    assert "/home/" not in blob
    assert pub["worker_pid"] is None
    assert pub["last_worker_pid"] == 1162285
    assert pub["active"] is False
    assert pub["coins"][0]["artifact_reference"] == "coin_runs/AAVEUSDT/def3dc3960ab4afa97c59e2c2ac444c7"
    assert public_status(status, None)["coins"][0]["artifact_reference"].find("..") == -1
    from stoch_fade_research_jobs.jobs import safe_artifact_reference

    assert safe_artifact_reference("coin_runs/../etc", symbol="AAVEUSDT", runner_run_id="x") is None


def test_frozen_blocks_update_and_releases_on_complete(tmp_path, monkeypatch):
    env, symbols, jobs = _env(tmp_path, monkeypatch)
    from stoch_universe_51.update_jobs import start_update_job
    from stoch_fade_research_jobs.jobs import load_job_public
    from stoch_fade_research_jobs.worker import run_job
    from stoch_heavy_job_gate import gate_path, read_gate

    payload, code = _post(env, ["ETHUSDT"], spawn=lambda a, c, l: 4242)
    assert code == 200
    coins = [
        {
            "symbol": "ETHUSDT",
            "coverage_status": "FULL",
            "testable": True,
            "freshness_status": "UPDATE_AVAILABLE",
            "data_from": "2025-12-11T00:00:00Z",
            "data_to": "2026-08-10T23:59:00Z",
            "candle_count": 100,
        }
    ]
    blocked, bcode = start_update_job(
        ["ETHUSDT"],
        environ=env,
        now=NOW,
        spawn=lambda a, c, l: 99,
        coverage_payload={"coins": coins},
    )
    assert bcode == 409
    assert blocked["error"] == "FROZEN_JOB_BLOCKS_CANDLE_UPDATE"
    assert read_gate(env) is not None
    run_job(job_dir_for(payload["job_id"], env))
    pub = load_job_public(payload["job_id"], env)
    assert pub["worker_pid"] is None
    assert pub["last_worker_pid"] == 4242
    assert pub["coins"][0]["warmup_complete_by_tf"]["15m"] is True
    assert "/home/" not in json.dumps(pub)
    assert not gate_path(env).exists()
    before = list(gate_path(env).parent.glob("*")) if gate_path(env).parent.exists() else []
    current_or_last_status(env, now=NOW)
    payload2, code2 = start_update_job(
        ["ETHUSDT"],
        environ=env,
        now=NOW,
        spawn=lambda a, c, l: 99,
        coverage_payload={"coins": coins},
    )
    assert code2 == 200


def test_orphan_heavy_gate_foreign_pid(tmp_path, monkeypatch):
    env, _, _ = _env(tmp_path, monkeypatch)
    import stoch_heavy_job_gate as heavy_gate
    from stoch_universe_51.jsonio import write_json_atomic

    path = heavy_gate.gate_path(env)
    write_json_atomic(
        path,
        {
            "owner_type": "FROZEN_RESEARCH",
            "job_id": "dead",
            "pid": 1,
            "started_at": "2020-01-01T00:00:00Z",
        },
    )
    monkeypatch.setattr("stoch_heavy_job_gate.worker_is_live", lambda pid, job_id, owner_type: False)
    monkeypatch.setattr("stoch_heavy_job_gate.pid_alive", lambda pid: True)
    assert heavy_gate.reconcile_gate(env) is None
    assert not path.exists()


def test_ui_cross_lock_and_warmup_copy():
    js = (DASHBOARD / "static" / "js" / "stoch_signale.js").read_text(encoding="utf-8")
    assert "frozenFadeJobActive()" in js
    assert "frozenFadeWarmupCell" in js
    assert "Artefaktfehler" in js
    assert "Noch nicht gestartet" in js
    assert 'state === "PENDING"' in js
    assert 'state === "INTERRUPTED"' in js
    assert "c.warmup_label" in js
    assert "setDesiredState" in js
    assert "universe51JobActive() || (typeof frozenFadeJobActive" in js

from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from pathlib import Path

from stoch_universe_51.coverage import (
    bump_coverage_generation,
    classify_symbol,
    clear_coverage_cache,
    coverage_generation,
    coverage_report,
    inclusive_minute_count,
    last_closed_open_time,
)
from stoch_universe_51.jsonio import write_json_atomic
from stoch_universe_51.origin import update_post_guard
from stoch_universe_51.update_jobs import (
    handle_update_post,
    public_status,
    reconcile_lock,
    start_update_job,
)
from stoch_universe_51.update_plan import (
    argv_for_call,
    last_closed_end_exclusive,
    plan_symbol_update,
    validate_update_symbols,
)
from stoch_universe_51.update_worker import run_job

REQUESTED = datetime(2025, 12, 11, tzinfo=timezone.utc)
NOW = datetime(2026, 8, 15, 7, 56, 30, tzinfo=timezone.utc)
DASHBOARD = Path(__file__).resolve().parents[2]
SG_UNIVERSE = Path(
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/"
    "signal_generator_stoch_waves/config/universe_tradeable_51.json"
)


def _env(tmp_path, monkeypatch, symbols=None):
    uni = tmp_path / "universe_tradeable_51.json"
    if symbols is None:
        symbols = ["ETHUSDT", "SOLUSDT", "LITUSDT"]
    uni.write_text(
        json.dumps(
            {
                "generated_at": "2026-08-11T00:00:00Z",
                "source": "test",
                "selection_method": "test",
                "target_size": len(symbols),
                "symbols": symbols,
            }
        ),
        encoding="utf-8",
    )
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    monkeypatch.setenv("STOCH_UNIVERSE_51_PATH", str(uni))
    monkeypatch.setenv("STOCH_UNIVERSE_51_JOBS_ROOT", str(jobs))
    monkeypatch.setenv("STOCH_UNIVERSE_51_CACHE_TTL", "60")
    monkeypatch.setenv("STOCH_UNIVERSE_51_BACKFILL_STUB", "1")
    monkeypatch.setenv("STOCH_FADE_RESEARCH_JOBS_ROOT", str(tmp_path / "fade_jobs"))
    monkeypatch.setenv("STOCH_DASHBOARD_START_GATE", str(tmp_path / "start.lock"))
    monkeypatch.setenv("STOCH_HEAVY_JOB_GATE", str(tmp_path / "heavy.lock"))
    return {
        "STOCH_UNIVERSE_51_PATH": str(uni),
        "STOCH_UNIVERSE_51_JOBS_ROOT": str(jobs),
        "STOCH_UNIVERSE_51_CACHE_TTL": "60",
        "STOCH_FADE_RESEARCH_JOBS_ROOT": str(tmp_path / "fade_jobs"),
        "STOCH_DASHBOARD_START_GATE": str(tmp_path / "start.lock"),
        "STOCH_HEAVY_JOB_GATE": str(tmp_path / "heavy.lock"),
    }


def _coin(symbol, *, data_to, data_from=None, freshness, coverage="FULL"):
    data_from = data_from or REQUESTED
    row = classify_symbol(
        symbol=symbol,
        requested_from=REQUESTED,
        data_from=data_from if coverage != "NO_DATA" else None,
        data_to=data_to if coverage != "NO_DATA" else None,
        candle_count=inclusive_minute_count(data_from, data_to) if coverage != "NO_DATA" else 0,
    )
    row["coverage_status"] = coverage
    row["freshness_status"] = freshness
    if coverage == "NO_DATA":
        row["data_from"] = None
        row["data_to"] = None
        row["testable"] = False
    return row


def _coverage(coins):
    return {"success": True, "coins": coins}


def test_validate_symbols(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch, ["ETHUSDT", "SOLUSDT"])
    from stoch_universe_51.config import universe_path
    from stoch_universe_51.universe import load_tradeable_51

    allowed = load_tradeable_51(universe_path(env))
    assert validate_update_symbols(["ETHUSDT"], allowed)[0] == ["ETHUSDT"]
    assert validate_update_symbols(["ETHUSDT", "SOLUSDT"], allowed)[1] is None
    assert validate_update_symbols([], allowed)[1] == "EMPTY_SYMBOLS"
    assert validate_update_symbols(["NOPEUSDT"], allowed)[1] == "UNKNOWN_SYMBOL"
    assert validate_update_symbols(["ETHUSDT", "ETHUSDT"], allowed)[1] == "DUPLICATE_SYMBOLS"
    assert validate_update_symbols(["ETHUSDT"] * 52, allowed)[1] == "TOO_MANY_SYMBOLS"
    assert validate_update_symbols(["../etc/passwd"], allowed)[1] == "UNKNOWN_SYMBOL"
    assert validate_update_symbols(["--cleanup-first"], allowed)[1] == "UNKNOWN_SYMBOL"


def test_all_51_symbols_accepted():
    from stoch_universe_51.universe import load_tradeable_51

    allowed = load_tradeable_51(SG_UNIVERSE)
    cleaned, err = validate_update_symbols(allowed, allowed)
    assert err is None
    assert cleaned == allowed
    assert len(cleaned) == 51


def test_plan_windows():
    last = last_closed_open_time(NOW)
    end_ex = last_closed_end_exclusive(NOW)
    assert end_ex == datetime(2026, 8, 15, 7, 56, tzinfo=timezone.utc)
    assert last.minute == 55

    current = _coin("APTUSDT", data_to=last, freshness="CURRENT")
    plan = plan_symbol_update(current, now=NOW)
    assert plan["action"] == "ALREADY_CURRENT"
    assert plan["calls"] == []

    stale = _coin(
        "ETHUSDT",
        data_to=datetime(2026, 8, 10, 23, 59, tzinfo=timezone.utc),
        freshness="UPDATE_AVAILABLE",
    )
    plan = plan_symbol_update(stale, now=NOW)
    assert plan["action"] == "NEEDS_UPDATE"
    assert plan["calls"][0]["start"] == "2026-08-11T00:00:00Z"
    assert plan["calls"][0]["end"] == "2026-08-15T07:56:00Z"
    assert plan["calls"][0]["repair_missing"] is True
    assert any(c["kind"] == "repair_missing" for c in plan["calls"])

    empty = _coin("NONEUSDT", data_to=last, freshness="NO_DATA", coverage="NO_DATA")
    plan = plan_symbol_update(empty, now=NOW)
    assert plan["calls"][0]["start"] == "2025-12-11T00:00:00Z"
    assert plan["calls"][0]["end"] == "2026-08-15T07:56:00Z"

    listing = _coin(
        "LITUSDT",
        data_from=datetime(2025, 12, 30, 13, 48, tzinfo=timezone.utc),
        data_to=datetime(2026, 8, 10, 23, 59, tzinfo=timezone.utc),
        freshness="UPDATE_AVAILABLE",
        coverage="LISTING_LIMITED",
    )
    plan = plan_symbol_update(listing, now=NOW)
    assert plan["listing_limited"] is True

    grace = _coin(
        "1000PEPEUSDT",
        data_to=datetime(2026, 8, 15, 7, 53, tzinfo=timezone.utc),
        freshness="CURRENT",
    )
    grace["lag_minutes"] = 2
    plan = plan_symbol_update(grace, now=NOW)
    assert plan["action"] == "ALREADY_CURRENT"
    assert plan["calls"] == []


def test_argv_shell_false_and_no_pipeline():
    argv = argv_for_call(
        python="/sg/.venv/bin/python",
        script="/sg/scripts/backfill_bybit_universe.py",
        universe_file="/jobs/j/universe_selected.json",
        symbol="ETHUSDT",
        start="2026-08-11T00:00:00Z",
        end="2026-08-15T07:56:00Z",
        out_dir="/jobs/j/backfill/ETHUSDT",
        checkpoint="/jobs/j/backfill/ETHUSDT/checkpoint.json",
        repair_missing=True,
        resume=False,
    )
    assert argv[1].endswith("backfill_bybit_universe.py")
    assert "--repair-missing" in argv
    assert "--cleanup-first" not in argv
    assert "run_wave_fade_shadow_pipeline.py" not in argv
    worker_src = (DASHBOARD / "stoch_universe_51" / "update_jobs.py").read_text(encoding="utf-8")
    assert "shell=False" in worker_src
    assert "Popen" in worker_src
    assert inspect.getsource(start_update_job)


def test_origin_and_json_guard():
    assert (
        update_post_guard(
            origin="http://dash.immotel.de:8080",
            referer=None,
            content_type="application/json",
        )
        is None
    )
    assert update_post_guard(origin=None, referer=None, content_type="application/json") == "ORIGIN_FORBIDDEN"
    assert (
        update_post_guard(
            origin="http://dash.immotel.de:8080",
            referer=None,
            content_type="text/plain",
        )
        == "JSON_CONTENT_TYPE_REQUIRED"
    )


def test_start_single_and_multi(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch)

    def spawn(argv, cwd, log_path):
        assert "update_worker.py" in argv[1]
        return 4242

    coins = [
        _coin(
            "ETHUSDT",
            data_to=datetime(2026, 8, 10, 23, 59, tzinfo=timezone.utc),
            freshness="UPDATE_AVAILABLE",
        ),
        _coin(
            "SOLUSDT",
            data_to=datetime(2026, 8, 10, 23, 59, tzinfo=timezone.utc),
            freshness="UPDATE_AVAILABLE",
        ),
        _coin(
            "LITUSDT",
            data_from=datetime(2025, 12, 30, 13, 48, tzinfo=timezone.utc),
            data_to=datetime(2026, 8, 10, 23, 59, tzinfo=timezone.utc),
            freshness="UPDATE_AVAILABLE",
            coverage="LISTING_LIMITED",
        ),
    ]
    payload, code = start_update_job(
        ["ETHUSDT"],
        environ=env,
        now=NOW,
        spawn=spawn,
        coverage_payload=_coverage(coins),
    )
    assert code == 200
    assert payload["success"] is True
    assert payload["symbols"] == ["ETHUSDT"]
    job_dir = Path(env["STOCH_UNIVERSE_51_JOBS_ROOT"]) / payload["job_id"]
    assert (job_dir / "request.json").exists()
    req = json.loads((job_dir / "request.json").read_text(encoding="utf-8"))
    assert req["repair_missing"] is True
    uni = json.loads((job_dir / "universe_selected.json").read_text(encoding="utf-8"))
    assert uni["symbols"] == ["ETHUSDT"]

    monkeypatch.setattr("stoch_universe_51.update_jobs.worker_is_live", lambda pid, job_id: True)
    payload2, code2 = start_update_job(
        ["ETHUSDT", "SOLUSDT"],
        environ=env,
        now=NOW,
        spawn=spawn,
        coverage_payload=_coverage(coins),
    )
    assert code2 == 409
    assert payload2["error"] == "UPDATE_JOB_ALREADY_RUNNING"

    monkeypatch.setattr("stoch_universe_51.update_jobs.worker_is_live", lambda pid, job_id: False)
    reconcile_lock(env)
    payload3, code3 = start_update_job(
        ["ETHUSDT", "SOLUSDT"],
        environ=env,
        now=NOW,
        spawn=spawn,
        coverage_payload=_coverage(coins),
    )
    assert code3 == 200
    assert payload3["symbols"] == ["ETHUSDT", "SOLUSDT"]


def test_handle_post_security(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch)
    coins = [
        _coin(
            "ETHUSDT",
            data_to=datetime(2026, 8, 10, 23, 59, tzinfo=timezone.utc),
            freshness="UPDATE_AVAILABLE",
        ),
    ]

    def spawn(argv, cwd, log_path):
        return 7

    body, code = handle_update_post(
        symbols=["ETHUSDT"],
        origin="http://evil.example",
        referer=None,
        content_type="application/json",
        environ=env,
        now=NOW,
        spawn=spawn,
        coverage_payload=_coverage(coins),
    )
    assert code == 403

    body, code = handle_update_post(
        symbols=["ETHUSDT"],
        origin="http://dash.immotel.de:8080",
        referer=None,
        content_type="application/json",
        extra_fields={"cli": "--cleanup-first"},
        environ=env,
        now=NOW,
        spawn=spawn,
        coverage_payload=_coverage(coins),
    )
    assert code == 400
    assert body["error"] == "UNKNOWN_FIELDS"


def test_worker_already_current_skips_downloader(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch)
    last = last_closed_open_time(NOW)
    coins = [_coin("ETHUSDT", data_to=last, freshness="CURRENT")]

    def spawn(argv, cwd, log_path):
        return 9

    payload, _code = start_update_job(
        ["ETHUSDT"],
        environ=env,
        now=NOW,
        spawn=spawn,
        coverage_payload=_coverage(coins),
    )
    job_dir = Path(env["STOCH_UNIVERSE_51_JOBS_ROOT"]) / payload["job_id"]
    rc = run_job(job_dir)
    log = (job_dir / "update.log").read_text(encoding="utf-8")
    assert "backfill_bybit_universe.py" not in log
    status = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "COMPLETED"
    assert status["coins"][0]["state"] == "ALREADY_CURRENT"
    assert rc == 0


def test_worker_success_partial_fail_and_cache(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch)
    coins = [
        _coin(
            "ETHUSDT",
            data_to=datetime(2026, 8, 10, 23, 59, tzinfo=timezone.utc),
            freshness="UPDATE_AVAILABLE",
        ),
        _coin(
            "SOLUSDT",
            data_to=datetime(2026, 8, 10, 23, 59, tzinfo=timezone.utc),
            freshness="UPDATE_AVAILABLE",
        ),
    ]

    def spawn(argv, cwd, log_path):
        return 11

    payload, _ = start_update_job(
        ["ETHUSDT", "SOLUSDT"],
        environ=env,
        now=NOW,
        spawn=spawn,
        coverage_payload=_coverage(coins),
    )
    job_dir = Path(env["STOCH_UNIVERSE_51_JOBS_ROOT"]) / payload["job_id"]
    gen0 = coverage_generation(env)

    def fake_spawn(argv, cwd, log_path):
        log_path.write_text("STUB " + " ".join(argv) + "\n", encoding="utf-8")
        if "ETHUSDT" in argv:
            return 0
        return 1

    monkeypatch.setattr("stoch_universe_51.update_worker._spawn_backfill", fake_spawn)
    run_job(job_dir)
    status = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "COMPLETED_WITH_ERRORS"
    by = {c["symbol"]: c["state"] for c in status["coins"]}
    assert by["ETHUSDT"] == "COMPLETED"
    assert by["SOLUSDT"] == "FAILED"
    assert coverage_generation(env) > gen0
    pub = public_status(status, json.loads((job_dir / "progress.json").read_text(encoding="utf-8")))
    blob = json.dumps(pub)
    assert "PASSWORD" not in blob
    log = (job_dir / "update.log").read_text(encoding="utf-8")
    assert "backfill_bybit_universe.py" in log
    assert "--repair-missing" in log
    assert "--cleanup-first" not in log
    assert "run_wave_fade" not in log


def test_worker_full_fail(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch)
    coins = [
        _coin(
            "ETHUSDT",
            data_to=datetime(2026, 8, 10, 23, 59, tzinfo=timezone.utc),
            freshness="UPDATE_AVAILABLE",
        ),
    ]

    def spawn(argv, cwd, log_path):
        return 11

    payload, _ = start_update_job(
        ["ETHUSDT"],
        environ=env,
        now=NOW,
        spawn=spawn,
        coverage_payload=_coverage(coins),
    )
    job_dir = Path(env["STOCH_UNIVERSE_51_JOBS_ROOT"]) / payload["job_id"]
    monkeypatch.setattr(
        "stoch_universe_51.update_worker._spawn_backfill",
        lambda argv, cwd, log_path: 9,
    )
    run_job(job_dir)
    status = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "FAILED"


def test_atomic_status_write(tmp_path):
    path = tmp_path / "status.json"
    write_json_atomic(path, {"job_id": "x", "state": "RUNNING"})
    assert json.loads(path.read_text(encoding="utf-8"))["state"] == "RUNNING"
    assert not list(tmp_path.glob("*.tmp"))


def test_cache_invalidation_file(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch)

    def fake_fetch(*, symbols, requested_from):
        return [], {"database": "signal_generator", "table": "candles_1m", "read_only": True}

    monkeypatch.setattr("stoch_universe_51.coverage._fetch_rows", fake_fetch)
    clear_coverage_cache()
    coverage_report(use_cache=True, environ=env, now=NOW)
    bump_coverage_generation(env)
    coverage_report(use_cache=True, environ=env, now=NOW)
    assert coverage_generation(env) >= 1


def test_app_routes_auth_and_get_cannot_start():
    src = (DASHBOARD / "app.py").read_text(encoding="utf-8")
    assert '@app.post("/api/stoch/universe-51-update")' in src
    assert '@app.get("/api/stoch/universe-51-update/status")' in src
    assert "Depends(require_auth)" in src
    assert "handle_update_post" in src
    assert src.count("@app.post(\"/api/stoch/universe-51-update\")") == 1


def test_frontend_buttons_and_strategy():
    html = (DASHBOARD / "templates" / "stoch_signale.html").read_text(encoding="utf-8")
    js = (DASHBOARD / "static" / "js" / "stoch_signale.js").read_text(encoding="utf-8")
    assert 'option value="wave_fade_no_be50_v1" selected' in html
    assert 'id="universe51UpdateSelected"' in html
    assert "Ausgewählte aktualisieren" in html
    assert "Aktion" in html
    assert "Aktualisieren" in js
    assert "universe51-update-one" in js
    assert "stopUniverse51JobPoll" in js
    assert "loadUniverse51Coverage(true)" in js
    assert "collectorControlCard" in html
    assert "Test starten" not in html
    assert "frozenFadeJobActive()" in js
    assert "FROZEN_JOB_BLOCKS_CANDLE_UPDATE" not in html


def test_frozen_job_blocks_candle_update(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch)
    coins = [
        _coin(
            "ETHUSDT",
            data_to=datetime(2026, 8, 10, 23, 59, tzinfo=timezone.utc),
            freshness="UPDATE_AVAILABLE",
        )
    ]
    monkeypatch.setattr(
        "stoch_fade_research_jobs.jobs.worker_is_live",
        lambda pid, job_id: True,
    )
    from stoch_fade_research_jobs.jobs import start_frozen_job

    fade, code = start_frozen_job(
        ["ETHUSDT"],
        "2025-12-11T00:00:00Z",
        "2026-08-15T07:56:00Z",
        environ=env,
        now=NOW,
        spawn=lambda a, c, l: 4242,
        coverage_payload={"coins": [{"symbol": "ETHUSDT", "coverage_status": "FULL", "testable": True}]},
        disk_free=10 * 1024 * 1024 * 1024,
    )
    assert code == 200
    payload, code = start_update_job(
        ["ETHUSDT"],
        environ=env,
        now=NOW,
        spawn=lambda a, c, l: 1,
        coverage_payload=_coverage(coins),
    )
    assert code == 409
    assert payload["error"] == "FROZEN_JOB_BLOCKS_CANDLE_UPDATE"
    assert payload["job_id"] == fade["job_id"]

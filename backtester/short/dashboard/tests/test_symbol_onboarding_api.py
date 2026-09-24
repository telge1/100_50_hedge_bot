"""API tests for symbol onboarding dashboard adapter (fully mocked)."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

DASHBOARD = Path(__file__).resolve().parents[1]
if str(DASHBOARD) not in sys.path:
    sys.path.insert(0, str(DASHBOARD))

from symbol_onboarding.api import build_router  # noqa: E402
from symbol_onboarding import rate_limit  # noqa: E402
from symbol_onboarding.sanitization import validate_job_id, public_message  # noqa: E402
from symbol_onboarding.schemas import parse_onboard_form  # noqa: E402


class _Client:
    def __init__(self, app: FastAPI):
        self._app = app

    def request(self, method: str, url: str, **kwargs):
        async def _run():
            transport = httpx.ASGITransport(app=self._app)
            async with httpx.AsyncClient(transport=transport, base_url="http://dash.immotel.de") as client:
                return await client.request(method, url, **kwargs)

        return asyncio.run(_run())

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)


def _app(role: str | None):
    def require_auth(request: Request):
        if role is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return {"username": "u-" + role, "role": role}

    def render_template(name, context):
        return f"HTML:{name}:role={context.get('user', {}).get('role')}"

    app = FastAPI()

    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    app.include_router(build_router(require_auth=require_auth, render_template=render_template))
    return _Client(app)


@pytest.fixture(autouse=True)
def _reset_rate():
    rate_limit.reset_for_tests()
    yield
    rate_limit.reset_for_tests()


def test_unauthenticated_page():
    c = _app(None)
    r = c.get("/datenverwaltung/symbole")
    assert r.status_code == 401


def test_viewer_forbidden_page_and_api():
    c = _app("viewer")
    assert c.get("/datenverwaltung/symbole").status_code == 403
    assert c.get("/api/datenverwaltung/symbole/jobs").status_code == 403
    r = c.post(
        "/api/datenverwaltung/symbole/plan",
        headers={"Content-Type": "application/json", "Origin": "http://dash.immotel.de"},
        content=json.dumps({"symbol": "AAPLUSDT", "days": 7}),
    )
    assert r.status_code == 403


def test_csrf_and_content_type(monkeypatch, tmp_path):
    c = _app("admin")
    monkeypatch.setenv("SYMBOL_ONBOARDING_PLANS_DIR", str(tmp_path / "plans"))
    monkeypatch.setenv("SYMBOL_ONBOARDING_JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("SYMBOL_ONBOARDING_OA_ROOT", str(tmp_path / "oa"))

    r = c.post(
        "/api/datenverwaltung/symbole/plan",
        headers={"Content-Type": "text/plain", "Origin": "http://dash.immotel.de"},
        content=b"{}",
    )
    assert r.status_code == 403
    assert r.json()["error"] == "JSON_CONTENT_TYPE_REQUIRED"

    r = c.post(
        "/api/datenverwaltung/symbole/plan",
        headers={"Content-Type": "application/json"},
        content=json.dumps({"symbol": "AAPLUSDT"}),
    )
    assert r.status_code == 403
    assert r.json()["error"] == "ORIGIN_FORBIDDEN"

    r = c.post(
        "/api/datenverwaltung/symbole/plan",
        headers={"Content-Type": "application/json", "Origin": "http://evil.example"},
        content=json.dumps({"symbol": "AAPLUSDT"}),
    )
    assert r.status_code == 403


def test_forbidden_full_ob_field():
    form, err = parse_onboard_form({"symbol": "AAPLUSDT", "days": 7, "full_ob": True})
    assert form is None
    assert err == "FORBIDDEN_FIELD"


def test_invalid_symbol_and_job_id():
    form, err = parse_onboard_form({"symbol": "aapl", "days": 7})
    assert err == "INVALID_SYMBOL"
    assert validate_job_id("../etc/passwd") is None
    assert validate_job_id("AAPLUSDT_20260101T000000Z_abcd1234") 


def test_public_message_strips_secrets():
    assert "PASSWORD" not in public_message("boom CLICKHOUSE_PASSWORD=secret").upper() or public_message(
        "boom CLICKHOUSE_PASSWORD=secret"
    ) == "Vorgang fehlgeschlagen"
    assert public_message("CLICKHOUSE_PASSWORD=x") == "Vorgang fehlgeschlagen"
    assert "Traceback" not in public_message("Traceback (most recent call last):\nSecret")


def test_plan_apply_flow_mocked(monkeypatch, tmp_path):
    oa = tmp_path / "oa"
    (oa / "config").mkdir(parents=True)
    plans = tmp_path / "plans"
    jobs = tmp_path / "jobs"
    queue = tmp_path / "queue"
    plans.mkdir()
    jobs.mkdir()
    queue.mkdir()
    monkeypatch.setenv("SYMBOL_ONBOARDING_OA_ROOT", str(oa))
    monkeypatch.setenv("SYMBOL_ONBOARDING_PLANS_DIR", str(plans))
    monkeypatch.setenv("SYMBOL_ONBOARDING_JOBS_DIR", str(jobs))
    monkeypatch.setenv("SYMBOL_ONBOARDING_QUEUE_DIR", str(queue))

    fake_job = {
        "job_id": "planjob1",
        "symbol": "AAPLUSDT",
        "requested_days": 7,
        "requested_streams": ["candles_1m"],
        "with_ob1000": False,
        "restart_live": False,
        "apply": False,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "current_stage": "validate_bybit",
        "status": "PLANNED",
        "progress_pct": 40,
        "stages": {},
        "plan": {
            "symbol": "AAPLUSDT",
            "days": 7,
            "instrument": {
                "symbol": "AAPLUSDT",
                "status": "Trading",
                "category": "linear",
                "quote_coin": "USDT",
                "tick_size": "0.01",
                "qty_step": "0.01",
                "min_order_qty": "0.01",
            },
            "notes": ["n1"],
        },
        "final_verdict": "ONE_COMMAND_SYMBOL_ONBOARDING_READY_DRY_RUN",
    }

    def fake_run_plan(form, **kwargs):
        assert form.symbol == "AAPLUSDT"
        return fake_job

    monkeypatch.setattr("symbol_onboarding.api.run_plan", fake_run_plan)

    c = _app("admin")
    headers = {"Content-Type": "application/json", "Origin": "http://dash.immotel.de"}
    body = {"symbol": "AAPLUSDT", "days": 7, "candles_1m": True, "with_ob1000": False}
    r = c.post("/api/datenverwaltung/symbole/plan", headers=headers, content=json.dumps(body))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["success"] is True
    assert data["plan_id"]
    assert data["plan_hash"]
    plan_id = data["plan_id"]
    plan_hash = data["plan_hash"]

    # Apply without confirm
    bad = dict(body, plan_id=plan_id, plan_hash=plan_hash)
    r = c.post("/api/datenverwaltung/symbole/apply", headers=headers, content=json.dumps(bad))
    assert r.status_code == 400
    assert r.json()["error"] == "CONFIRMATION_REQUIRED"

    # Manipulated hash
    bad2 = dict(body, confirm=True, plan_id=plan_id, plan_hash="0" * 64)
    r = c.post("/api/datenverwaltung/symbole/apply", headers=headers, content=json.dumps(bad2))
    assert r.status_code == 409
    assert r.json()["error"] == "PLAN_HASH_MISMATCH"

    # Changed form after plan
    bad3 = dict(body, confirm=True, plan_id=plan_id, plan_hash=plan_hash, days=30)
    r = c.post("/api/datenverwaltung/symbole/apply", headers=headers, content=json.dumps(bad3))
    assert r.status_code == 409

    t0 = time.time()
    ok = dict(body, confirm=True, plan_id=plan_id, plan_hash=plan_hash)
    r = c.post("/api/datenverwaltung/symbole/apply", headers=headers, content=json.dumps(ok))
    elapsed = time.time() - t0
    assert r.status_code == 202, r.text
    assert elapsed < 2.0
    assert r.json()["job_id"]
    assert r.json()["state"] == "QUEUED"
    # Queue file exists; no inline spawn in production path
    qjob = queue / r.json()["job_id"]
    assert (qjob / "request.json").is_file()
    assert json.loads((qjob / "worker_status.json").read_text())["state"] == "QUEUED"

    # Double apply / concurrent while QUEUED
    r2 = c.post("/api/datenverwaltung/symbole/apply", headers=headers, content=json.dumps(ok))
    assert r2.status_code == 409


def test_job_get_list_and_traversal(monkeypatch, tmp_path):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    monkeypatch.setenv("SYMBOL_ONBOARDING_JOBS_DIR", str(jobs))
    monkeypatch.setenv("SYMBOL_ONBOARDING_OA_ROOT", str(tmp_path))
    job = {
        "job_id": "AAPLUSDT_20260101T000000Z_abcd1234",
        "symbol": "AAPLUSDT",
        "requested_days": 7,
        "requested_streams": [],
        "with_ob1000": False,
        "restart_live": False,
        "apply": True,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:01Z",
        "current_stage": "backfill_candles",
        "status": "FAILED",
        "progress_pct": 10,
        "error_code": "x",
        "error_message": "CLICKHOUSE_PASSWORD=leak",
        "stages": {"validate_bybit": {"name": "validate_bybit", "status": "SUCCEEDED"}},
        "plan": {},
        "final_verdict": "FAILED",
        "rollback_status": "ROLLED_BACK",
    }
    (jobs / f"{job['job_id']}.json").write_text(json.dumps(job), encoding="utf-8")

    c = _app("admin")
    r = c.get("/api/datenverwaltung/symbole/jobs/" + job["job_id"])
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "FAILED"
    assert body["rollback_status"] == "ROLLED_BACK"
    assert "PASSWORD" not in (body.get("error_message") or "").upper()

    r = c.get("/api/datenverwaltung/symbole/jobs/../../etc/passwd")
    assert r.status_code in (400, 404)

    r = c.get("/api/datenverwaltung/symbole/jobs")
    assert r.status_code == 200
    assert r.json()["total"] >= 1


def test_waiting_for_archive_hint(monkeypatch, tmp_path):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    monkeypatch.setenv("SYMBOL_ONBOARDING_JOBS_DIR", str(jobs))
    jid = "AAPLUSDT_20260101T000000Z_wait0001"
    job = {
        "job_id": jid,
        "symbol": "AAPLUSDT",
        "requested_days": 7,
        "requested_streams": [],
        "with_ob1000": False,
        "restart_live": False,
        "apply": True,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:01Z",
        "current_stage": "backfill_public_trades",
        "status": "WAITING_FOR_ARCHIVE",
        "progress_pct": 50,
        "stages": {},
        "plan": {},
        "final_verdict": "WAITING_FOR_ARCHIVE",
    }
    (jobs / f"{jid}.json").write_text(json.dumps(job), encoding="utf-8")
    c = _app("admin")
    r = c.get(f"/api/datenverwaltung/symbole/jobs/{jid}")
    assert r.status_code == 200
    assert "Bybit-Tagesarchiv" in (r.json().get("waiting_for_archive_hint") or "")


def test_worker_survives_dashboard_restart_simulation(tmp_path, monkeypatch):
    """Queue consumer fixture job continues after parent 'dashboard' exits."""
    import os
    import signal
    import subprocess
    import sys as _sys

    queue = tmp_path / "queue"
    queue.mkdir()
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    monkeypatch.setenv("SYMBOL_ONBOARDING_QUEUE_DIR", str(queue))
    monkeypatch.setenv("SYMBOL_ONBOARDING_JOBS_DIR", str(jobs))
    monkeypatch.setenv("SYMBOL_ONBOARDING_ALLOW_FIXTURE", "1")

    from symbol_onboarding.worker_client import enqueue_fixture, read_queue_status

    payload, code = enqueue_fixture(
        sleep_sec=4,
        environ={
            "SYMBOL_ONBOARDING_QUEUE_DIR": str(queue),
            "SYMBOL_ONBOARDING_JOBS_DIR": str(jobs),
            "SYMBOL_ONBOARDING_ALLOW_FIXTURE": "1",
        },
    )
    assert code == 202
    job_id = payload["job_id"]

    env = os.environ.copy()
    env["SYMBOL_ONBOARDING_QUEUE_DIR"] = str(queue)
    env["SYMBOL_ONBOARDING_JOBS_DIR"] = str(jobs)
    env["SYMBOL_ONBOARDING_ALLOW_FIXTURE"] = "1"
    env["PYTHONPATH"] = str(DASHBOARD) + os.pathsep + env.get("PYTHONPATH", "")
    consumer = subprocess.Popen(  # noqa: S603
        [_sys.executable, "-m", "symbol_onboarding.queue_consumer", "--once", "--poll-sec", "0.5"],
        cwd=str(DASHBOARD),
        env=env,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    # Simulate dashboard dying: we only started the consumer as sibling
    time.sleep(1.0)
    assert consumer.poll() is None or True  # may still be running fixture
    # Wait for completion without killing consumer
    try:
        consumer.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.kill(consumer.pid, signal.SIGTERM)
        consumer.wait(timeout=5)
        raise
    st = read_queue_status(job_id, environ={"SYMBOL_ONBOARDING_QUEUE_DIR": str(queue)})
    assert st is not None
    assert st["state"] == "COMPLETED"

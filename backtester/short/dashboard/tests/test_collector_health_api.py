"""API-level tests without Starlette TestClient (httpx app= incompat)."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from collector_health.api import build_router
from collector_health.csrf import COOKIE_NAME, issue_csrf_token


def _auth(request: Request):
    if request.cookies.get("session_id") != "ok":
        return JSONResponse({"detail": "auth"}, status_code=401)
    return {"username": "tester"}


async def _call(app: FastAPI, method: str, path: str, **kwargs):
    """Minimal ASGI call helper."""
    import json as _json

    body = kwargs.get("json")
    headers = [(k.lower().encode(), str(v).encode()) for k, v in (kwargs.get("headers") or {}).items()]
    cookies = kwargs.get("cookies") or {}
    if cookies:
        cookie_hdr = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers.append((b"cookie", cookie_hdr.encode()))
    raw = b""
    if body is not None:
        raw = _json.dumps(body).encode()
        headers.append((b"content-type", b"application/json"))
        headers.append((b"content-length", str(len(raw)).encode()))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 123),
        "server": ("test", 80),
    }
    messages = []

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    body_parts = [m.get("body", b"") for m in messages if m["type"] == "http.response.body"]
    payload = b"".join(body_parts)
    try:
        data = _json.loads(payload.decode() or "null")
    except Exception:
        data = payload.decode()
    return start["status"], data


def _app():
    app = FastAPI()

    async def require_auth(request: Request):
        if request.cookies.get("session_id") != "ok":
            from fastapi import HTTPException

            raise HTTPException(status_code=401, detail="auth")
        return {"username": "tester"}

    app.include_router(build_router(require_auth=require_auth))
    return app


def test_health_requires_auth():
    app = _app()
    status, _ = asyncio.run(_call(app, "GET", "/api/collector-health"))
    assert status == 401


def test_unknown_collector():
    app = _app()
    status, data = asyncio.run(
        _call(app, "GET", "/api/collector-health/nope", cookies={"session_id": "ok"})
    )
    assert status == 404
    assert data["error"] == "UNKNOWN_COLLECTOR_ID"


def test_detect_requires_csrf_and_origin(monkeypatch):
    app = _app()
    monkeypatch.setattr(
        "collector_health.api.start_job",
        lambda **kwargs: ({"success": True, "job_id": "x"}, 202),
    )
    tok = issue_csrf_token()
    status_bad, data_bad = asyncio.run(
        _call(
            app,
            "POST",
            "/api/collector-backfill/detect",
            json={
                "symbols": ["BTCUSDT"],
                "start": "2026-09-01T00:00:00Z",
                "end": "2026-09-02T00:00:00Z",
            },
            cookies={"session_id": "ok", COOKIE_NAME: tok},
            headers={"Origin": "https://evil.example", "Content-Type": "application/json"},
        )
    )
    assert status_bad == 403

    status_good, data_good = asyncio.run(
        _call(
            app,
            "POST",
            "/api/collector-backfill/detect",
            json={
                "symbols": ["BTCUSDT"],
                "start": "2026-09-01T00:00:00Z",
                "end": "2026-09-02T00:00:00Z",
            },
            cookies={"session_id": "ok", COOKIE_NAME: tok},
            headers={
                "Origin": "https://dash.immotel.de",
                "Content-Type": "application/json",
                "X-CSRF-Token": tok,
            },
        )
    )
    assert status_good == 202
    assert data_good["success"] is True


def test_pt_button_path_blocked():
    app = _app()
    tok = issue_csrf_token()
    status, data = asyncio.run(
        _call(
            app,
            "POST",
            "/api/collector-backfill/start",
            json={
                "collector_id": "public_trades_live",
                "symbols": ["BTCUSDT"],
                "start": "2026-09-01T00:00:00Z",
                "end": "2026-09-02T00:00:00Z",
            },
            cookies={"session_id": "ok", COOKIE_NAME: tok},
            headers={
                "Origin": "https://dash.immotel.de",
                "Content-Type": "application/json",
                "X-CSRF-Token": tok,
            },
        )
    )
    assert status == 409
    assert "PUBLIC_TRADES_BLOCKED" in data["error"]

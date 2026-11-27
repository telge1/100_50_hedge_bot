"""MC4O-2 — scoped pane gzip middleware (isolated ASGI)."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

DASHBOARD_ROOT = Path(__file__).resolve().parents[2]
import sys

sys.path.insert(0, str(DASHBOARD_ROOT))

from research_charts.api import build_router  # noqa: E402
from research_charts.pane_gzip_middleware_v1 import (  # noqa: E402
    ResearchPaneGzipMiddleware,
    research_pane_gzip_enabled,
)

WEEKS12_5M = max(100, round((12 * 7 * 24 * 3600) / 300))


class _Client:
    def __init__(self, app: FastAPI):
        self.app = app

    def request(self, method: str, path: str, **kwargs):
        async def _go():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(_go())

    def raw_post(self, path: str, json_body: dict, headers: dict):
        """Collect raw ASGI response bytes (bypasses httpx Content-Encoding decode)."""
        async def _go():
            payload = json.dumps(json_body).encode("utf-8")
            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "headers": [
                    (k.lower().encode(), v.encode())
                    for k, v in headers.items()
                ]
                + [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())],
                "scheme": "http",
                "server": ("testserver", 80),
                "client": ("127.0.0.1", 12345),
            }
            messages: list[dict] = []
            sent = False

            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": payload, "more_body": False}
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                messages.append(message)

            await self.app(scope, receive, send)
            status = 500
            resp_headers: dict[str, str] = {}
            body = b""
            for msg in messages:
                if msg["type"] == "http.response.start":
                    status = msg["status"]
                    resp_headers = {
                        k.decode(): v.decode() for k, v in msg.get("headers") or []
                    }
                elif msg["type"] == "http.response.body":
                    body += msg.get("body") or b""
            return status, resp_headers, body

        return asyncio.run(_go())


def _build_test_app() -> FastAPI:
    def _auth():
        return {"username": "mc4o2"}

    def _render(name, context):
        return "<html>research</html>"

    application = FastAPI()
    application.add_middleware(ResearchPaneGzipMiddleware)
    application.include_router(build_router(require_auth=_auth, render_template=_render))
    return application


@pytest.fixture
def pane_app():
    return _build_test_app()


@pytest.fixture
def gzip_headers():
    return {"Accept-Encoding": "gzip, deflate"}


def _pane_body(symbol: str, *, limit: int = WEEKS12_5M, lld: bool = True) -> dict:
    return {
        "symbol": symbol,
        "timeframe": "5m",
        "limit": limit,
        "liquidity": {"enabled": lld, "amount": 2200},
        "ema": {"enabled": True, "lines": []},
        "stochastic": {"enabled": False},
        "open_interest": {"enabled": False},
    }


def _small_pane_body(symbol: str = "SOLUSDT") -> dict:
    return {
        "symbol": symbol,
        "timeframe": "5m",
        "limit": 120,
        "liquidity": {"enabled": False},
        "ema": {"enabled": False},
        "stochastic": {"enabled": False},
        "open_interest": {"enabled": False},
    }


def test_flag_off_no_gzip(pane_app, monkeypatch):
    monkeypatch.delenv("RESEARCH_PANE_GZIP_ENABLED", raising=False)
    assert not research_pane_gzip_enabled()
    client = _Client(pane_app)
    res = client.request(
        "POST",
        "/api/research/pane",
        json=_pane_body("SOLUSDT"),
        headers={"Accept-Encoding": "gzip, deflate"},
    )
    assert res.status_code == 200
    assert res.headers.get("content-encoding") is None
    assert len(res.content) >= 1_048_576


def test_flag_on_gzip_large_pane(pane_app, gzip_headers, monkeypatch):
    monkeypatch.setenv("RESEARCH_PANE_GZIP_ENABLED", "1")
    assert research_pane_gzip_enabled()
    client = _Client(pane_app)
    body = _pane_body("SOLUSDT", lld=True)
    status, headers, wire = client.raw_post(
        "/api/research/pane",
        body,
        gzip_headers,
    )
    assert status == 200
    assert headers.get("content-encoding") == "gzip"
    assert "Accept-Encoding" in (headers.get("vary") or "")
    assert wire[:2] == b"\x1f\x8b"
    raw = gzip.decompress(wire)
    assert len(raw) >= 1_048_576
    payload = json.loads(raw)
    assert payload.get("success") is True
    assert payload.get("symbol") == "SOLUSDT"


def test_flag_on_no_accept_encoding_plain(pane_app, monkeypatch):
    monkeypatch.setenv("RESEARCH_PANE_GZIP_ENABLED", "1")
    client = _Client(pane_app)
    res = client.request(
        "POST",
        "/api/research/pane",
        json=_pane_body("SOLUSDT"),
        headers={"Accept-Encoding": "identity"},
    )
    assert res.status_code == 200
    assert res.headers.get("content-encoding") is None
    assert res.content[:1] == b"{"


def test_flag_on_under_min_size(pane_app, gzip_headers, monkeypatch):
    monkeypatch.setenv("RESEARCH_PANE_GZIP_ENABLED", "1")
    client = _Client(pane_app)
    res = client.request(
        "POST",
        "/api/research/pane",
        json=_small_pane_body(),
        headers=gzip_headers,
    )
    assert res.status_code == 200
    assert res.headers.get("content-encoding") is None
    assert len(res.content) < 1_048_576


def test_other_route_unaffected(pane_app, gzip_headers, monkeypatch):
    monkeypatch.setenv("RESEARCH_PANE_GZIP_ENABLED", "1")
    client = _Client(pane_app)
    res = client.request("GET", "/api/research/symbols", headers=gzip_headers)
    assert res.status_code == 200
    assert res.headers.get("content-encoding") is None


def test_flag_unknown_off(monkeypatch):
    monkeypatch.setenv("RESEARCH_PANE_GZIP_ENABLED", "yes")
    assert not research_pane_gzip_enabled()


def _fingerprint_payload(payload: dict) -> dict:
    candles = payload.get("candles") or []
    closed = candles[:-1] if len(candles) > 1 else list(candles)
    return {
        "symbol": payload.get("symbol"),
        "timeframe": payload.get("timeframe"),
        "candles_n": len(candles),
        "closed_n": len(closed),
        "first_closed": closed[0] if closed else None,
        "last_closed": closed[-1] if closed else None,
        "closed_sha256": hashlib.sha256(
            json.dumps(closed, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest(),
    }


@pytest.mark.parametrize("symbol", ["DOGEUSDT", "SOLUSDT", "BNBUSDT"])
def test_byte_parity_off_vs_gunzip_on(symbol, pane_app, monkeypatch):
    """Gzip roundtrip preserves closed-candle OHLCV; live tip may shift between calls."""
    identity = {"Accept-Encoding": "identity"}
    gzip_headers = {"Accept-Encoding": "gzip, deflate"}
    client = _Client(pane_app)
    body = _pane_body(symbol, lld=True)
    monkeypatch.delenv("RESEARCH_PANE_GZIP_ENABLED", raising=False)
    _, _, plain_bytes = client.raw_post("/api/research/pane", body, identity)
    plain = json.loads(plain_bytes)

    monkeypatch.setenv("RESEARCH_PANE_GZIP_ENABLED", "1")
    _, headers, wire = client.raw_post("/api/research/pane", body, gzip_headers)
    assert headers.get("content-encoding") == "gzip"
    assert wire[:2] == b"\x1f\x8b"
    decoded = gzip.decompress(wire)
    gz = json.loads(decoded)
    assert _fingerprint_payload(plain) == _fingerprint_payload(gz)


def test_middleware_static_byte_roundtrip():
    """Middleware gzip/gunzip on fixed bytes (deterministic transport parity)."""
    import anyio

    sample = (b'{"success":true,"data":"' + b"x" * (MIN_SIZE := 1_048_576 + 50)) + b'"}'

    async def fake_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": sample, "more_body": False})

    async def run():
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/research/pane",
            "headers": [(b"accept-encoding", b"gzip")],
        }
        out: list[dict] = []

        async def send(msg):
            out.append(msg)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        wrapped = ResearchPaneGzipMiddleware(fake_app)
        os.environ["RESEARCH_PANE_GZIP_ENABLED"] = "1"
        try:
            await wrapped(scope, receive, send)
        finally:
            os.environ.pop("RESEARCH_PANE_GZIP_ENABLED", None)
        body = b""
        for msg in out:
            if msg["type"] == "http.response.body":
                body = msg.get("body") or b""
        return body

    wire = anyio.run(run)
    assert wire[:2] == b"\x1f\x8b"
    assert gzip.decompress(wire) == sample


def test_auth_error_unchanged(pane_app, gzip_headers, monkeypatch):
    monkeypatch.setenv("RESEARCH_PANE_GZIP_ENABLED", "1")

    class _NoAuthApp(FastAPI):
        pass

    def _deny():
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="unauthorized")

    app = FastAPI()
    app.add_middleware(ResearchPaneGzipMiddleware)
    app.include_router(build_router(require_auth=_deny, render_template=lambda n, c: ""))
    client = _Client(app)
    res = client.request(
        "POST",
        "/api/research/pane",
        json=_small_pane_body(),
        headers=gzip_headers,
    )
    assert res.status_code == 401
    assert res.headers.get("content-encoding") is None
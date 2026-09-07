"""In-process FastAPI smoke: router once, static mount before /static."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

DASHBOARD_DIR = Path(__file__).resolve().parents[2]
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

from footprint_candles.api import build_router  # noqa: E402


class _AsgiClient:
    def __init__(self, app):
        self._app = app

    def get(self, url):
        async def _run():
            transport = httpx.ASGITransport(app=self._app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                return await client.get(url)

        return asyncio.run(_run())


def _app_with_mount_order():
    """Mirror production: footprint static BEFORE catch-all /static."""
    app = FastAPI()

    def _auth():
        return {"username": "fp"}

    app.include_router(build_router(require_auth=_auth))
    fp_static = DASHBOARD_DIR / "footprint_candles" / "static"
    static_dir = DASHBOARD_DIR / "static"
    app.mount(
        "/static/footprint_candles",
        StaticFiles(directory=str(fp_static)),
        name="static_footprint_candles",
    )
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    return _AsgiClient(app)


def test_meta_and_static_reachable_with_correct_mount_order():
    c = _app_with_mount_order()
    meta = c.get("/api/footprint-candles/meta")
    assert meta.status_code == 200
    body = meta.json()
    assert body["limits"]["max_range_seconds"] == 21600
    assert body["limits"]["max_closed_candles_5m"] == 72
    assert body["limits"]["max_candles"] == 73

    js = c.get("/static/footprint_candles/footprint_candles.js")
    assert js.status_code == 200
    assert b"FootprintCandles" in js.content
    assert b"formingCandleWindow" in js.content

    css = c.get("/static/footprint_candles/footprint_candles.css")
    assert css.status_code == 200
    assert b"pointer-events: none" in css.content

    # Existing MP static still served via catch-all /static
    mp = c.get("/static/market_profile_v1/app.js")
    assert mp.status_code == 200
    assert b"FOOTPRINT_HOOK" in mp.content or b"market" in mp.content.lower()


def test_wrong_mount_order_would_404_footprint_js():
    """Document the shadowing bug: /static first hides module assets."""
    app = FastAPI()

    def _auth():
        return {"username": "fp"}

    app.include_router(build_router(require_auth=_auth))
    static_dir = DASHBOARD_DIR / "static"
    fp_static = DASHBOARD_DIR / "footprint_candles" / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    app.mount(
        "/static/footprint_candles",
        StaticFiles(directory=str(fp_static)),
        name="static_footprint_candles",
    )
    c = _AsgiClient(app)
    js = c.get("/static/footprint_candles/footprint_candles.js")
    # With /static first, Starlette serves from dashboard/static/... → 404
    assert js.status_code == 404


def test_router_registered_once_in_routes():
    app = FastAPI()
    app.include_router(build_router(require_auth=lambda: {"username": "x"}))
    paths = [getattr(r, "path", None) for r in app.routes]
    assert paths.count("/api/footprint-candles") == 1
    assert paths.count("/api/footprint-candles/meta") == 1

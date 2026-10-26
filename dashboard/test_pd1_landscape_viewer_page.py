"""PD1 Landscape Viewer dashboard integration."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from jinja2 import Environment, FileSystemLoader

DASHBOARD_ROOT = Path(__file__).resolve().parent
if str(DASHBOARD_ROOT) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_ROOT))

from pd1_landscape_viewer_v1.api import build_router  # noqa: E402


class _AsgiClient:
    def __init__(self, app):
        self._app = app

    def get(self, url: str):
        async def _run():
            transport = httpx.ASGITransport(app=self._app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.get(url)

        return asyncio.run(_run())


def _mini():
    env = Environment(loader=FileSystemLoader(DASHBOARD_ROOT / "templates"), autoescape=True)

    def _auth():
        return {"username": "viewer-test"}

    def _render(name, context):
        return env.get_template(name).render(**context)

    app = FastAPI()
    app.include_router(build_router(require_auth=_auth, render_template=_render))
    return _AsgiClient(app)


def test_nav_includes_pd1_landscape_viewer():
    nav = (DASHBOARD_ROOT / "templates/partials/nav.html").read_text(encoding="utf-8")
    assert 'href="/live-charts/viewer"' in nav
    assert "PD1 Landscape Viewer" in nav
    assert nav.index("Market Profile") < nav.index("PD1 Landscape Viewer")


def test_pd1_viewer_route_and_assets():
    client = _mini()
    page = client.get("/live-charts/viewer")
    assert page.status_code == 200
    assert "PD1 Landscape Viewer" in page.text
    assert '/live-charts/viewer" class="nav-dropdown-link active"' in page.text.replace("\n", " ")
    assert "PD1_VIEWER_BASE" in page.text
    assert 'id="gotoUtc"' in page.text
    assert 'id="btnGoToUtc"' in page.text
    assert "GO TO UTC" in page.text

    js = client.get("/live-charts/viewer/replay.js")
    assert js.status_code == 200
    assert "dataUrl" in js.text
    assert "goToUtcFromInput" in js.text
    assert "ensureLoadedForGoto" in js.text
    assert "ResearchGotoTime" in js.text
    assert "updateNavStatusBar" in js.text
    assert "goto_time.js" in page.text
    assert "panTimeByDelta" in js.text
    assert "drawCandlesticks" in js.text
    assert "drawWhiteClosePriceLine" in js.text
    assert 'strokeStyle = "#ffffff"' not in js.text
    assert "parseGoToUtcInput" in js.text

    meta = client.get("/live-charts/viewer/data/meta.json")
    assert meta.status_code == 200
    assert "period_start" in meta.text


def test_pd1_go_to_utc_window_vs_research_dates():
    """Bundle window is static JSON; Sep 2026 GO TO must fail until bundle is extended."""
    from pd1_landscape_viewer_v1 import VIEWER_ROOT

    meta_path = VIEWER_ROOT / "data/meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    start = meta["period_start"]
    end = meta["period_end"]
    for iso in ("2026-09-05T10:00:00Z", "2026-09-15T01:45:00Z"):
        assert iso < start or iso > end, f"{iso} outside bundle {start}..{end}"


def test_pd1_viewer_requires_auth_for_data():
    app = FastAPI()

    def _deny():
        from fastapi import HTTPException

        raise HTTPException(status_code=401)

    app.include_router(build_router(require_auth=_deny, render_template=lambda n, c: ""))
    client = _AsgiClient(app)
    res = client.get("/live-charts/viewer/data/meta.json")
    assert res.status_code == 401

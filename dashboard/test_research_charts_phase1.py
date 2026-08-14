"""Phase 1 Research Charts: navigation, routes, auth, no PySide in web runtime."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

DASHBOARD_ROOT = Path(__file__).resolve().parent

import sys

sys.path.insert(0, str(DASHBOARD_ROOT))

from research_charts.api import build_router  # noqa: E402
from research_charts.boundary import (  # noqa: E402
    DESKTOP_ONLY,
    FORBIDDEN_WEB_IMPORTS,
    PHASE_1_FEED_READY,
    SUPPORTED_LAYOUTS,
)
from research_charts.demo import demo_candles  # noqa: E402


class _AsgiClient:
    def __init__(self, app: FastAPI, cookies: dict[str, str] | None = None):
        self.app = app
        self.cookies = dict(cookies or {})

    def get(self, path: str, follow_redirects: bool = True, params: dict | None = None):
        async def _go():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                follow_redirects=follow_redirects,
            ) as client:
                for key, value in self.cookies.items():
                    client.cookies.set(key, value)
                return await client.get(path, params=params)

        return asyncio.run(_go())


def _mini_client() -> _AsgiClient:
    def _auth():
        return {"username": "phase1"}

    def _render(name: str, context: dict) -> str:
        assert name == "research_charts.html"
        assert context["user"]["username"] == "phase1"
        return "<html>Research Charts PAGE Hedge Bot Charts Live Charts</html>"

    mini = FastAPI()
    mini.include_router(build_router(require_auth=_auth, render_template=_render))
    return _AsgiClient(mini)


def _dashboard_app():
    mod = sys.modules.get("app")
    if mod is not None and not hasattr(mod, "sessions"):
        del sys.modules["app"]
    if str(DASHBOARD_ROOT) not in sys.path:
        sys.path.insert(0, str(DASHBOARD_ROOT))
    import app as dashboard_app

    return dashboard_app


def _real_client(authenticated: bool) -> _AsgiClient:
    dashboard_app = _dashboard_app()
    dashboard_app.sessions["phase1-session"] = {"username": "phase1"}
    cookies = {"session_id": "phase1-session"} if authenticated else {}
    return _AsgiClient(dashboard_app.app, cookies=cookies)


def test_demo_candles_are_utc_unix_seconds():
    rows = demo_candles("APTUSDT", "5m", limit=40)
    assert len(rows) == 40
    times = [r["time"] for r in rows]
    assert times == sorted(times)
    assert all((t % 300) == 0 for t in times)
    assert all({"open", "high", "low", "close", "volume", "time"} <= set(r) for r in rows)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_no_pyside_or_desktop_imports_in_dashboard_research():
    research_dir = DASHBOARD_ROOT / "research_charts"
    py_files = [p for p in research_dir.rglob("*.py") if p.name != "PHASE1.md"]
    assert py_files
    for path in py_files:
        imported = _imported_modules(path)
        assert "PySide6" not in imported
        assert "PyQt6" not in imported
        assert "PyQt5" not in imported
        assert "app" not in imported
    js = (DASHBOARD_ROOT / "static" / "js" / "research" / "research_charts.js").read_text(
        encoding="utf-8"
    )
    assert "PySide6" not in js
    assert "qwebchannel" not in js.lower()
    assert FORBIDDEN_WEB_IMPORTS  # documented boundary still exists


def test_research_python_parses_without_trp():
    for path in (DASHBOARD_ROOT / "research_charts").rglob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"))


def test_research_page_renders():
    client = _mini_client()
    res = client.get("/live-charts/research")
    assert res.status_code == 200
    assert "Research Charts" in res.text


def test_research_api_contracts():
    client = _mini_client()
    symbols = client.get("/api/research/symbols").json()
    assert symbols["success"] is True
    assert symbols["feed_ready"] is True
    assert symbols["source"] == "clickhouse_candles_1m"
    names = {s["symbol"] for s in symbols["symbols"]}
    assert names

    sample = next(iter(names))
    candles = client.get(
        "/api/research/candles",
        params={"symbol": sample, "timeframe": "15m", "limit": 50},
    )
    body = candles.json()
    assert candles.status_code == 200
    assert body["source"] == "clickhouse_candles_1m"
    assert body["timeframe"] == "15m"
    assert body["aggregation"] == "trp_aggregate_strict"
    assert len(body["candles"]) > 0

    stream = client.get("/api/research/stream").json()
    assert stream["ready"] is False
    assert stream["proposed_path"] == "/api/research/stream"

    meta = client.get("/api/research/meta").json()
    assert meta["layouts"] == list(SUPPORTED_LAYOUTS)
    assert meta["source"] == "clickhouse_candles_1m"
    assert meta["realtime_mode"] == "forming_1m_poll"

    inds = client.get(
        "/api/research/indicators",
        params={"symbol": sample, "timeframe": "15m", "limit": 80, "ema": True},
    ).json()
    assert inds["compute_in"] == "python"
    assert inds["symbol"] == sample
    assert inds["ema"]["series"]


def test_live_charts_parent_nav_and_submenu_in_partial():
    nav = (DASHBOARD_ROOT / "templates" / "partials" / "nav.html").read_text(encoding="utf-8")
    assert "📈 Live Charts" in nav
    assert 'href="/live-charts/hedge"' in nav
    assert "Hedge Bot Charts" in nav
    assert 'href="/live-charts/research"' in nav
    assert "Research Charts" in nav
    assert 'class="nav-dropdown' in nav
    # Research is submenu, not a sibling top-level item besides Live Charts
    assert nav.count("Research Charts") == 1


def test_live_charts_redirect_and_hedge_unchanged():
    client = _real_client(authenticated=True)
    redir = client.get("/live-charts", follow_redirects=False)
    assert redir.status_code == 302
    assert redir.headers["location"].startswith("/live-charts/hedge")

    redir_q = client.get("/live-charts?account=main&symbol=APTUSDT", follow_redirects=False)
    assert redir_q.status_code == 302
    loc = redir_q.headers["location"]
    assert loc.startswith("/live-charts/hedge")
    assert "account=main" in loc
    assert "symbol=APTUSDT" in loc

    hedge = client.get("/live-charts/hedge", follow_redirects=False)
    assert hedge.status_code == 200
    assert "Live Chart für" in hedge.text
    assert 'id="liveChart"' in hedge.text
    assert 'action="/live-charts"' in hedge.text
    assert "Hedge Bot Charts" in hedge.text
    assert "Research Charts" in hedge.text


def test_research_route_on_real_app():
    client = _real_client(authenticated=True)
    page = client.get("/live-charts/research")
    assert page.status_code == 200
    assert "Research Charts" in page.text
    assert "researchWorkspace" in page.text
    assert "Liquidity Location" in page.text
    assert "Hedge Bot Charts" in page.text


def test_auth_preserved_on_research_and_hedge():
    client = _real_client(authenticated=False)
    assert client.get("/live-charts/research").status_code == 401
    assert client.get("/live-charts/hedge").status_code == 401
    assert client.get("/live-charts", follow_redirects=False).status_code == 401
    assert client.get("/api/research/symbols").status_code == 401
    assert client.get("/api/research/candles", params={"symbol": "APTUSDT"}).status_code == 401
    assert client.get("/api/research/stream").status_code == 401


def test_phase1_boundary_flag_kept_for_docs():
    assert PHASE_1_FEED_READY is False

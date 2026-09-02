"""TRP chart.js parity: renderer port, drawings, settings, no duplicate chart engine."""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI

DASHBOARD_ROOT = Path(__file__).resolve().parent
import sys

sys.path.insert(0, str(DASHBOARD_ROOT))

from research_charts.api import build_router  # noqa: E402
from research_charts.workspace_session import (  # noqa: E402
    PANE_IDS,
    TOOLS,
    reset_workspace_for_tests,
)

TRP_JS = DASHBOARD_ROOT / "static" / "research_trp" / "chart.js"
TRP_CSS = DASHBOARD_ROOT / "static" / "research_trp" / "style.css"
VENDOR = DASHBOARD_ROOT / "static" / "research_trp" / "vendor" / "lightweight-charts.min.js"
HOST_JS = DASHBOARD_ROOT / "static" / "js" / "research" / "research_charts.js"
PANE_HTML = DASHBOARD_ROOT / "static" / "research_trp" / "pane.html"
PAGE_HTML = DASHBOARD_ROOT / "templates" / "research_charts.html"


class _AsgiClient:
    def __init__(self, app: FastAPI, cookies: dict[str, str] | None = None):
        self.app = app
        self.cookies = dict(cookies or {})

    def _call(self, method: str, path: str, **kwargs):
        async def _go():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
                follow_redirects=kwargs.pop("follow_redirects", True),
            ) as client:
                for key, value in self.cookies.items():
                    client.cookies.set(key, value)
                return await getattr(client, method)(path, **kwargs)

        return asyncio.run(_go())

    def get(self, path: str, **kwargs):
        return self._call("get", path, **kwargs)

    def post(self, path: str, **kwargs):
        return self._call("post", path, **kwargs)

    def put(self, path: str, **kwargs):
        return self._call("put", path, **kwargs)


def _mini():
    def _auth():
        return {"username": "parity"}

    def _render(name, context):
        return PAGE_HTML.read_text(encoding="utf-8")

    app = FastAPI()
    app.include_router(build_router(require_auth=_auth, render_template=_render))
    return _AsgiClient(app)


def _ws(tmp_path, monkeypatch):
    import research_charts.workspace_session as ws_mod

    monkeypatch.setattr(ws_mod, "USER_DATA_DIR", tmp_path)
    monkeypatch.setattr(ws_mod, "DRAWINGS_PATH", tmp_path / "drawings.json")
    monkeypatch.setattr(ws_mod, "SETTINGS_PATH", tmp_path / "indicator_settings.json")
    return reset_workspace_for_tests()


def test_trp_chart_js_loaded_without_hard_qwebchannel():
    js = TRP_JS.read_text(encoding="utf-8")
    assert "overlayRegistry" in js
    assert "function renderZone" in js
    assert "function resetView" in js
    assert "priceToCoordinate" in js
    assert "timeToCoordinate" in js
    assert "function snapUnixToBar" in js
    assert "scaleWatchRaf" in js
    assert "layoutOverlays" in js
    assert "setSelectedMarker" in js
    assert "setInteractionMode" in js
    assert "startShiftMeasure" in js
    assert "lastAppliedSize" in js
    assert "window.chartApi" in js
    assert "if (window.bridge)" in js
    connect = js[js.index("function connectBridge") : js.index("function debugInfo")]
    assert "setTimeout(connectBridge" in connect
    assert "typeof QWebChannel === \"function\"" in connect


def test_lightweight_charts_423_not_v5():
    vendor = VENDOR.read_text(encoding="utf-8", errors="ignore")
    assert "v4.2.3" in vendor
    page = PAGE_HTML.read_text(encoding="utf-8")
    host = HOST_JS.read_text(encoding="utf-8")
    assert "lightweight-charts@5" not in page
    assert "unpkg.com/lightweight-charts" not in page
    assert "createChart" not in host
    assert "destroyCharts" not in host
    assert "research_trp/pane.html" in host
    assert "research_trp/chart.js" in PANE_HTML.read_text(encoding="utf-8")
    assert "qwebchannel" not in PANE_HTML.read_text(encoding="utf-8").lower()
    assert "#131722" in TRP_CSS.read_text(encoding="utf-8")
    assert "#3dcc91" in TRP_CSS.read_text(encoding="utf-8")


def test_host_is_bridge_not_renderer():
    host = HOST_JS.read_text(encoding="utf-8")
    assert "pollGen" in host
    assert "stopPoll" in host
    assert "LIVE NOT AVAILABLE" in host
    assert "COLLECTOR UNAVAILABLE" in host
    assert "bybit.com" not in host.lower()
    assert "WebSocket" not in host
    assert "qwebchannel" not in host.lower()
    assert "pooled-hidden" in host
    assert "PANE_IDS" in host
    assert "resetView" in host
    assert "setSyncedCrosshair" in host
    assert "setSelectedMarker" in host
    for tool in TOOLS:
        assert tool in host
    assert "createPriceLine" not in host


def test_toolbar_and_pane_pool_in_page():
    page = PAGE_HTML.read_text(encoding="utf-8")
    assert "trp-workspace" in page
    assert 'data-layout="1"' in page
    assert 'data-layout="2H"' in page
    assert 'data-layout="2V"' in page
    assert 'data-layout="4"' in page
    assert "modalEma" in page
    assert "modalStoch" in page
    assert "modalLld" in page
    assert "modalPos" in page
    assert "researchLiveBar" in page
    assert "K Length" in page
    assert "Show Individual Pool Borders" in page
    assert PANE_IDS == ("pane-0", "pane-1", "pane-2", "pane-3")


def test_no_pyside_in_research_python():
    research_dir = DASHBOARD_ROOT / "research_charts"
    for path in research_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "PySide6" not in imported
        assert "app" not in imported


def test_drawing_tools_compose_and_persistence(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    ts_a = datetime(2026, 2, 1, 12, 0, tzinfo=timezone.utc)
    ts_b = datetime(2026, 2, 1, 12, 15, tzinfo=timezone.utc)
    ws.set_drawing_tool("hline")
    ws.on_point(pane_id="pane-0", timeframe="5m", symbol="APTUSDT", ts=ts_a, price=1.5)
    assert ws.snapshot()["tool"] == "select"
    ws.set_drawing_tool("vline")
    ws.on_point(pane_id="pane-0", timeframe="5m", symbol="APTUSDT", ts=ts_a, price=1.5)
    ws.set_drawing_tool("trend")
    ws.on_point(pane_id="pane-0", timeframe="5m", symbol="APTUSDT", ts=ts_a, price=1.4)
    assert ws.snapshot()["pending"] is True
    assert ws.snapshot()["tool"] == "trend"
    ws.on_point(pane_id="pane-0", timeframe="5m", symbol="APTUSDT", ts=ts_b, price=1.6)
    assert ws.snapshot()["tool"] == "select"
    ws.set_drawing_tool("rectangle")
    ws.on_point(pane_id="pane-0", timeframe="5m", symbol="APTUSDT", ts=ts_a, price=1.3)
    ws.on_point(pane_id="pane-0", timeframe="5m", symbol="APTUSDT", ts=ts_b, price=1.7)
    ws.set_drawing_tool("measure")
    ws.on_point(pane_id="pane-0", timeframe="5m", symbol="APTUSDT", ts=ts_a, price=1.4)
    ws.on_point(pane_id="pane-0", timeframe="5m", symbol="APTUSDT", ts=ts_b, price=1.8)
    ws.set_drawing_tool("long_position")
    ws.on_point(pane_id="pane-0", timeframe="5m", symbol="APTUSDT", ts=ts_a, price=1.4)
    ws.on_point(pane_id="pane-0", timeframe="5m", symbol="APTUSDT", ts=ts_b, price=1.2)
    ws.set_drawing_tool("short_position")
    ws.on_point(pane_id="pane-0", timeframe="5m", symbol="APTUSDT", ts=ts_a, price=1.6)
    ws.on_point(pane_id="pane-0", timeframe="5m", symbol="APTUSDT", ts=ts_b, price=1.8)
    types = {d.drawing_type for d in ws.drawings.get_drawings("APTUSDT")}
    assert types >= {
        "hline",
        "vline",
        "trend",
        "rectangle",
        "measure",
        "long_position",
        "short_position",
    }
    payloads = ws.composed_overlays("APTUSDT", "5m")
    kinds = {p["type"] for p in payloads}
    assert "line" in kinds
    assert "zone" in kinds
    assert "label" in kinds
    assert "position" in kinds
    measure = next(d for d in ws.drawings.get_drawings("APTUSDT") if d.drawing_type == "measure")
    assert measure.created_on_timeframe == "5m"
    hline = next(d for d in ws.drawings.get_drawings("APTUSDT") if d.drawing_type == "hline")
    ws.on_hit(hline.drawing_id + ":line")
    assert ws.snapshot()["selected_id"] == hline.drawing_id
    ws.on_drag(hline.drawing_id + ":line", None, 2.25, {})
    assert ws.drawings.get_drawing(hline.drawing_id).price == 2.25
    ws.delete_selected()
    assert hline.drawing_id not in ws.drawings
    ws.set_drawing_tool("trend")
    ws.on_point(pane_id="pane-0", timeframe="5m", symbol="APTUSDT", ts=ts_a, price=1.1)
    assert ws.snapshot()["pending"] is True
    ws.cancel_drawing()
    assert ws.snapshot()["tool"] == "select"
    assert ws.snapshot()["pending"] is False
    ws.clear_drawings("APTUSDT")
    assert ws.drawings.get_drawings("APTUSDT") == []
    assert (tmp_path / "drawings.json").is_file()


def test_settings_modal_semantics_and_persistence(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    defaults = ws.settings_defaults()
    assert defaults["stochastic"]["k_length"] == 14
    assert defaults["stochastic"]["overbought_level"] == 80
    assert defaults["liquidity"]["show_pool_borders"] is True
    assert defaults["liquidity"]["clusters_enabled"] is True
    assert len(defaults["ema"]["lines"]) == 3
    ws.apply_settings(
        stochastic={
            "enabled": True,
            "k_length": 21,
            "k_smoothing": 2,
            "d_smoothing": 4,
            "overbought_level": 75,
            "oversold_level": 25,
            "show_k": True,
            "show_d": False,
            "show_levels": True,
            "k_color": "#5b8def",
            "d_color": "#ef9f27",
            "level_color": "#6b7388",
        }
    )
    assert ws.stoch_config.k_length == 21
    assert ws.stoch_config.show_d is False
    ws2 = reset_workspace_for_tests()
    assert ws2.stoch_config.k_length == 21


def test_workspace_http_and_no_duplicate_renderer():
    client = _mini()
    page = client.get("/live-charts/research")
    assert page.status_code == 200
    assert "Research Charts" in page.text
    assert "unpkg.com/lightweight-charts@5" not in page.text
    snap = client.get("/api/research/workspace").json()
    assert snap["success"] is True
    assert "hline" in snap["tools"]
    assert "measure" in snap["tools"]
    assert "long_position" in snap["tools"]
    defs = client.get("/api/research/settings/defaults").json()
    assert "ema" in defs and "stochastic" in defs and "liquidity" in defs
    host = HOST_JS.read_text(encoding="utf-8")
    assert host.count("chartApi") >= 1
    assert "LightweightCharts" not in host


def test_drawing_http_hline_and_esc(tmp_path, monkeypatch):
    _ws(tmp_path, monkeypatch)
    client = _mini()
    client.post("/api/research/drawings/tool", json={"tool": "hline"})
    body = client.post(
        "/api/research/drawings/event",
        json={"type": "point", "pane_id": "pane-0", "timeframe": "5m", "symbol": "APTUSDT", "price": 1.11},
    ).json()
    assert body["selected_id"]
    esc = client.post("/api/research/drawings/event", json={"type": "escape"}).json()
    assert esc["tool"] == "select"
    cleared = client.post("/api/research/drawings/clear", json={"symbol": "APTUSDT"}).json()
    assert cleared["selected_id"] is None


def test_zone_rendering_contract_in_trp_js():
    js = TRP_JS.read_text(encoding="utf-8")
    css = TRP_CSS.read_text(encoding="utf-8")
    assert "ov-zone" in js and "ov-zone" in css
    assert "clipOverlayLayerToPlot" in js
    assert "plotRightX" in js
    assert "plotBottomY" in js
    assert "timeAxisHeight" in js
    assert "onShiftMeasureMoveCapture" in js
    assert "extend_right" in js or "extendRight" in js or "extend_right" in js
    assert "ov-position" in js and "ov-handle" in js
    assert "DEFAULT_VISIBLE_BARS" in js
    assert "DEFAULT_BAR_SPACING" in js


def test_collector_strings_regression():
    host = HOST_JS.read_text(encoding="utf-8")
    assert "ensure=false" in host
    assert "ensure=true" in host
    html = (DASHBOARD_ROOT / "templates" / "research_charts.html").read_text()
    assert "research_charts.js?v=ob-levels-2" in html
    assert "researchHeightHandle" in html
    assert "researchChartDock" in html
    assert "researchDockBar" in html
    assert "research.css?v=history-3" in html
    assert "researchIndStoch" in html
    assert "researchIndLld" in html
    dock = html[html.index("researchDockBar") : html.index("researchWorkspace")]
    assert "researchIndStoch" in dock
    assert "researchIndLld" in dock
    assert "researchBacktesterBtn" in html
    assert "researchFullscreenBtn" in html
    assert "researchBacktesterBtn" in host
    assert "/api/research/backtester/load" in host
    assert "strategy_version" in host
    assert "/api/research/live-status" in host
    assert "/api/research/candles" in host
    py = (DASHBOARD_ROOT / "research_charts" / "clickhouse_source.py").read_text()
    assert "candles_1m" in py


def test_symbols_api_returns_clickhouse_list():
    client = _mini()
    res = client.get("/api/research/symbols")
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    names = [row["symbol"] for row in body["symbols"]]
    assert len(names) >= 50
    assert names == sorted(names)
    assert "APTUSDT" in names
    assert "DOGEUSDT" in names
    assert "ETHUSDT" in names


def test_symbol_selector_boot_order_and_default():
    host = HOST_JS.read_text(encoding="utf-8")
    page = PAGE_HTML.read_text(encoding="utf-8")
    css = (DASHBOARD_ROOT / "static" / "css" / "research.css").read_text(encoding="utf-8")
    assert 'id="researchSymbol"' in page
    assert "function fillSymbolSelect" in host
    assert "function pickDefaultSymbol" in host
    assert host.index('getJson("/api/research/symbols")') < host.index(
        'getJson("/api/research/workspace")'
    )
    assert "body.detail" in host
    assert "clearOverlays" in host
    assert "#researchSymbol" in css
    assert "min-width" in css
    boot = host[host.index("async function boot") :]
    assert "fillSymbolSelect" in boot
    assert "pickDefaultSymbol" in boot
    assert "switchSymbol(start)" in boot


def test_symbol_change_loads_candles_without_not_found():
    client = _mini()
    page = client.get("/live-charts/research")
    assert page.status_code == 200
    assert "Not Found" not in page.text
    assert 'id="researchSymbol"' in page.text
    for symbol in ("APTUSDT", "DOGEUSDT", "ETHUSDT"):
        candles = client.get(
            "/api/research/candles",
            params={"symbol": symbol, "timeframe": "5m", "limit": 20},
        )
        assert candles.status_code == 200, symbol
        body = candles.json()
        assert body["symbol"] == symbol
        assert body["candles"]
        assert "Not Found" not in candles.text


def test_symbol_api_auth_preserved_and_static_pane_ok():
    mod = sys.modules.get("app")
    if mod is not None and not hasattr(mod, "sessions"):
        del sys.modules["app"]
    if str(DASHBOARD_ROOT) not in sys.path:
        sys.path.insert(0, str(DASHBOARD_ROOT))
    import app as dashboard_app

    anon = _AsgiClient(dashboard_app.app)
    assert anon.get("/api/research/symbols").status_code == 401
    assert anon.get("/api/research/symbols").json()["detail"] == "Not authenticated"
    pane = anon.get("/static/research_trp/pane.html")
    assert pane.status_code == 200
    assert "Not Found" not in pane.text
    assert "chart.js" in pane.text

    dashboard_app.sessions["sym-sel"] = {"username": "sym"}
    auth = _AsgiClient(dashboard_app.app, cookies={"session_id": "sym-sel"})
    body = auth.get("/api/research/symbols").json()
    assert body["success"] is True
    assert len(body["symbols"]) >= 50

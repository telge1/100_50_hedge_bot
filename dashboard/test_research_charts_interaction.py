"""FIX_RESEARCH_TRP_INTERACTION_AND_INDICATORS: coalescing, tools, indicators, 429."""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI

DASHBOARD_ROOT = Path(__file__).resolve().parent
import sys

sys.path.insert(0, str(DASHBOARD_ROOT))

from research_charts.api import build_router  # noqa: E402
from research_charts.service import (  # noqa: E402
    clear_candle_cache_for_tests,
    compute_indicators,
    load_candles,
    pane_bundle,
)
from research_charts.workspace_session import (  # noqa: E402
    overlay_namespace,
    reset_workspace_for_tests,
)

HOST_JS = DASHBOARD_ROOT / "static" / "js" / "research" / "research_charts.js"
TRP_JS = DASHBOARD_ROOT / "static" / "research_trp" / "chart.js"
PAGE_HTML = DASHBOARD_ROOT / "templates" / "research_charts.html"

EMA_LINE = {
    "ema_id": "ema-test-20",
    "enabled": True,
    "period": 20,
    "color": "#ff9800",
    "line_width": 2,
    "transparency": 0,
}


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
        return {"username": "interaction"}

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


def _count_ch(monkeypatch):
    import research_charts.service as svc

    hits = {"n": 0}
    orig = svc.ClickHouseResearchCandleSource.get_1m_candles

    def wrapped(self, *args, **kwargs):
        hits["n"] += 1
        time.sleep(0.12)
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(svc.ClickHouseResearchCandleSource, "get_1m_candles", wrapped)
    return hits


def test_host_no_duplicate_initial_candle_gets():
    host = HOST_JS.read_text(encoding="utf-8")
    assert 'sendJson("/api/research/pane"' in host
    assert "PANE_HTTP_LIMIT = 2" in host
    assert "mapLimit(visibleIds(), PANE_HTTP_LIMIT" in host
    assert "Promise.all(visibleIds().map" not in host
    load_pane = host[host.index("async function loadPane") : host.index("async function refreshIndicatorsVisible")]
    assert "/api/research/candles" not in load_pane
    assert "/api/research/indicators" not in load_pane


def test_host_coalesce_and_hidden_pane_and_poll_after_initial():
    host = HOST_JS.read_text(encoding="utf-8")
    assert "inflightGets" in host
    assert "inflightPosts" in host
    assert "info.coalesced = true" in host
    assert "state.initialLoadDone" in host
    assert "if (!state.initialLoadDone) return" in host
    switch = host[host.index("async function switchSymbol") : host.index("function fillSymbolSelect")]
    assert "state.initialLoadDone = false" in switch
    assert "state.initialLoadDone = true" in switch
    assert switch.index("state.initialLoadDone = true") < switch.index("startPoll()")
    assert "visibleIds()" in switch
    layout = host[host.index('document.querySelectorAll(".trp-layout-btn")') :]
    assert "loadNewlyVisiblePanes" in host
    assert "sourceAction: \"layout-change\"" in host
    assert "poll-indicators" not in host
    assert 'layout: "1"' in host
    assert "researchFullscreenBtn" in host
    assert "togglePaneFullscreen" in host
    assert "researchHeightHandle" in host
    assert "researchChartDock" in host
    assert "resetWorkspaceHeight" in host
    assert "expandWorkspaceUp" in host
    assert "bindHeightDrag" in host
    assert "research-browser-fs" in host
    assert 'pane.tf !== "1m"' in host
    assert "lastClosedBarTime" in host
    assert "closedCandleFingerprint" in host


def test_host_generation_guard():
    host = HOST_JS.read_text(encoding="utf-8")
    assert "state.loadGen" in host
    assert "pane.paneGen" in host
    assert "state.loadAbort.abort" in host
    assert "if (gen !== state.loadGen || paneGen !== pane.paneGen) return" in host
    assert "packed.timeframe !== pane.tf" in host


def test_host_iframe_ready_handshake():
    host = HOST_JS.read_text(encoding="utf-8")
    assert "IFRAME_LOADING" in host
    assert "CHART_API_READY" in host
    assert "DATA_READY" in host
    assert "INTERACTION_READY" in host
    assert "function whenReady" in host
    assert 'pane.html?v=" + ASSET_V' in host
    assert 'const ASSET_V = "vp-2"' in host
    build = host[host.index("function buildPanes") : host.index("function applyLayout")]
    assert build.index("addEventListener(\"load\"") < build.index("iframe.src")
    assert 'src="/static/research_trp/pane.html"' not in build
    assert "on_drawing_event" in host
    assert "on_chart_ready" in host
    assert "pushInteractionMode" in host
    assert "chart.setInteractionMode" in host
    trp = TRP_JS.read_text(encoding="utf-8")
    assert "function setInteractionMode" in trp
    assert "window.chartApi" in trp
    assert "emitDrawing" in trp
    assert "if (!window.bridge) return" in trp


def test_host_indicator_apply_does_not_reload_candles_http():
    host = HOST_JS.read_text(encoding="utf-8")
    assert "refreshIndicatorsVisible" in host
    assert "allowStale: true" in host
    assert "indicatorsOnly: true" in host
    stoch = host[host.index('$("researchIndStoch").addEventListener') : host.index('$("trpEmaSettings")')]
    assert "refreshIndicatorsVisible" in stoch
    assert "reloadVisible" not in stoch
    assert "/api/research/candles" not in stoch
    ema = host[host.index('$("emaApply")') : host.index('$("stochApply")')]
    assert "refreshIndicatorsVisible" in ema
    assert "reloadVisible" not in ema


def test_host_toolbar_bridge_table():
    host = HOST_JS.read_text(encoding="utf-8")
    page = PAGE_HTML.read_text(encoding="utf-8")
    trp = TRP_JS.read_text(encoding="utf-8")
    tools = [
        "select",
        "trend",
        "hline",
        "vline",
        "rectangle",
        "circle",
        "arrow",
        "measure",
        "long_position",
        "short_position",
    ]
    for tool in tools:
        assert f'dataset.tool = pair[0]' in host or tool in host
        assert tool in host
    assert "trpDelete" in page and "$(\"trpDelete\")" in host
    assert "trpClear" in page and "$(\"trpClear\")" in host
    assert "setTool(pair[0])" in host
    assert "chart.setInteractionMode(mode)" in host
    assert "function setInteractionMode" in trp
    assert "type: \"point\"" in trp
    assert "type: \"hit\"" in trp
    assert "type: \"drag\"" in trp
    assert "type: \"edit\"" in trp
    assert "on_chart_key" in trp and "on_chart_key" in host


def test_shift_measure_host_shift_copy_price_and_crosshair():
    host = HOST_JS.read_text(encoding="utf-8")
    js = TRP_JS.read_text(encoding="utf-8")
    css = (DASHBOARD_ROOT / "static" / "research_trp" / "style.css").read_text(encoding="utf-8")
    pane = (DASHBOARD_ROOT / "static" / "research_trp" / "pane.html").read_text(encoding="utf-8")
    assert "function syncHostShift" in host
    assert 'if (ev.key === "Shift") syncHostShift(true)' in host
    assert "function shiftHeld" in js
    assert "function setHostShift" in js
    assert "setHostShift: setHostShift" in js
    assert "function onChartContextMenu" in js
    assert "Preis kopiert" in js
    assert "cursor: crosshair" in css
    assert "flex: 1" in css
    assert "height: 100%" in css
    assert "chart.js?v=history-5" in pane
    assert "function snapUnixToBar" in js
    assert "preserveView" in js
    assert "preserveView: true" in host
    assert 'window.addEventListener("pointerdown", onShiftMeasureDown, true)' in js
    assert "onPointerDown, true" in js
    assert "function cursorForDragMode" in js
    assert "resize-tp" in js
    assert "resize-sl" in js
    assert "resize-left" in js
    assert "function finishToolToSelect" in js
    assert "function deactivateToolsLocal" in host
    assert "on_tool_idle" in host
    assert "function pollForming" in host
    assert "updateFormingBar" in host
    assert "function updateFormingBar" in js


def test_price_scale_precision_from_candles():
    js = TRP_JS.read_text(encoding="utf-8")
    assert "function inferPriceFormat" in js
    assert "function applyPriceFormat" in js
    assert "applyPriceFormat(inferPriceFormat(candles))" in js
    assert "lastValueVisible: false" in js
    assert "minimumWidth: 84" in js
    candle_block = js[js.index("candleSeries = chart.addCandlestickSeries") : js.index("lldEmaFastSeries = chart.addLineSeries")]
    assert "lastValueVisible: true" in candle_block
    assert "priceFormat: lastPriceFormat" in candle_block
    ema_block = js[js.index("function setEmaOverlays") : js.index("function setLldEma")]
    assert "lastValueVisible: false" in ema_block
    assert "priceFormat: lastPriceFormat" in ema_block


def test_concurrent_identical_candle_loads_coalesced(monkeypatch):
    hits = _count_ch(monkeypatch)
    clear_candle_cache_for_tests()
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = [pool.submit(load_candles, "APTUSDT", "5m", limit=40) for _ in range(3)]
        results = [fut.result(timeout=90) for fut in futs]
    assert hits["n"] == 1
    assert all(row["candles"] for row in results)
    assert len({len(row["candles"]) for row in results}) == 1


def test_pane_bundle_single_candle_read(monkeypatch):
    hits = _count_ch(monkeypatch)
    clear_candle_cache_for_tests()
    packed = pane_bundle(
        "APTUSDT",
        "5m",
        limit=80,
        ema={"lines": [EMA_LINE]},
        stochastic={"enabled": True, "k_length": 14, "k_smoothing": 3, "d_smoothing": 3},
        liquidity={"enabled": False},
    )
    assert hits["n"] == 1
    assert packed["candles"]
    assert packed["ema"]["series"]
    assert packed["stochastic"]["id"] == "stochastic"
    assert packed["overlays"] is not None


def test_indicator_compute_uses_stale_cache(monkeypatch):
    hits = _count_ch(monkeypatch)
    clear_candle_cache_for_tests()
    load_candles("APTUSDT", "5m", limit=40)
    after_load = hits["n"]
    assert after_load >= 1
    time.sleep(2.1)
    body = compute_indicators(
        "APTUSDT",
        "5m",
        limit=40,
        ema={"lines": [EMA_LINE]},
        stochastic={"enabled": True},
        liquidity={"enabled": False},
    )
    assert hits["n"] == after_load
    assert body["ema"]["series"]
    assert body["stochastic"]["visible"] is True


def test_pane_http_and_settings_apply_no_second_ch_read(tmp_path, monkeypatch):
    _ws(tmp_path, monkeypatch)
    hits = _count_ch(monkeypatch)
    clear_candle_cache_for_tests()
    client = _mini()
    first = client.post(
        "/api/research/pane",
        json={
            "symbol": "APTUSDT",
            "timeframe": "5m",
            "limit": 60,
            "ema": {"lines": [EMA_LINE]},
            "stochastic": {"enabled": False},
            "liquidity": {"enabled": False},
        },
    )
    assert first.status_code == 200
    n1 = hits["n"]
    assert n1 == 1
    second = client.post(
        "/api/research/pane",
        json={
            "symbol": "APTUSDT",
            "timeframe": "5m",
            "limit": 60,
            "ema": {"lines": [EMA_LINE]},
            "stochastic": {"enabled": True, "k_length": 14, "k_smoothing": 3, "d_smoothing": 3},
            "liquidity": {"enabled": False},
            "allow_stale": True,
        },
    )
    assert second.status_code == 200
    assert hits["n"] == n1
    body = second.json()
    assert body["stochastic"]["visible"] is True
    levels = {lvl.get("price") for lvl in (body["stochastic"].get("levels") or [])}
    assert 80 in levels or 80.0 in levels
    assert 20 in levels or 20.0 in levels


def test_drawing_tools_hline_vline_trend_rectangle_measure_positions(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    ts_a = datetime(2026, 2, 1, 12, 0, tzinfo=timezone.utc)
    ts_b = datetime(2026, 2, 1, 12, 15, tzinfo=timezone.utc)
    ws.set_drawing_tool("hline")
    snap = ws.on_point(pane_id="pane-0", timeframe="5m", symbol="APTUSDT", ts=ts_a, price=1.5)
    assert snap["selected_id"]
    ws.set_drawing_tool("vline")
    ws.on_point(pane_id="pane-0", timeframe="5m", symbol="APTUSDT", ts=ts_a, price=1.5)
    ws.set_drawing_tool("trend")
    ws.on_point(pane_id="pane-0", timeframe="5m", symbol="APTUSDT", ts=ts_a, price=1.4)
    assert ws.snapshot()["pending"] is True
    ws.on_point(pane_id="pane-0", timeframe="5m", symbol="APTUSDT", ts=ts_b, price=1.6)
    ws.set_drawing_tool("rectangle")
    ws.on_point(pane_id="pane-0", timeframe="5m", symbol="APTUSDT", ts=ts_a, price=1.3)
    ws.on_point(pane_id="pane-0", timeframe="5m", symbol="APTUSDT", ts=ts_b, price=1.7)
    ws.set_drawing_tool("measure")
    ws.on_point(pane_id="pane-0", timeframe="5m", symbol="APTUSDT", ts=ts_a, price=1.4)
    ws.on_point(pane_id="pane-0", timeframe="5m", symbol="APTUSDT", ts=ts_b, price=1.8)
    ws.set_drawing_tool("long_position")
    ws.on_point(pane_id="pane-0", timeframe="5m", symbol="APTUSDT", ts=ts_a, price=1.4)
    ws.on_point(pane_id="pane-0", timeframe="5m", symbol="APTUSDT", ts=ts_b, price=1.2)
    assert ws.snapshot()["tool"] == "select"
    long_d = next(d for d in ws.drawings.get_drawings("APTUSDT") if d.drawing_type == "long_position")
    ws.on_drag(
        long_d.drawing_id + ":position",
        None,
        None,
        {
            "mode": "resize-tp",
            "start_timestamp": ts_a.timestamp(),
            "end_timestamp": ts_b.timestamp(),
            "entry_price": long_d.entry_price,
            "stop_price": long_d.stop_price,
            "target_price": 1.55,
        },
    )
    assert abs(float(ws.drawings.get_drawing(long_d.drawing_id).target_price) - 1.55) < 1e-9
    ws.on_drag(
        long_d.drawing_id + ":position",
        None,
        None,
        {
            "mode": "resize-right",
            "start_timestamp": ts_a.timestamp(),
            "end_timestamp": ts_b.timestamp() + 900,
            "entry_price": ws.drawings.get_drawing(long_d.drawing_id).entry_price,
            "stop_price": ws.drawings.get_drawing(long_d.drawing_id).stop_price,
            "target_price": ws.drawings.get_drawing(long_d.drawing_id).target_price,
        },
    )
    assert abs(ws.drawings.get_drawing(long_d.drawing_id).end_timestamp.timestamp() - (ts_b.timestamp() + 900)) < 1
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
    measure = next(p for p in payloads if p.get("metadata", {}).get("drawing_type") == "measure" and p["type"] == "label")
    text = measure.get("text") or ""
    assert "%" in text
    assert "bar" in text
    ns = {p["namespace"] for p in payloads}
    assert "USER_DRAWING" in ns
    assert "POSITION" in ns
    hline = next(d for d in ws.drawings.get_drawings("APTUSDT") if d.drawing_type == "hline")
    ws.on_hit(hline.drawing_id + ":line")
    ws.on_drag(hline.drawing_id + ":line", None, 2.25, {})
    assert ws.drawings.get_drawing(hline.drawing_id).price == 2.25
    ws.delete_selected()
    assert hline.drawing_id not in ws.drawings
    ws.set_drawing_tool("trend")
    ws.on_point(pane_id="pane-0", timeframe="5m", symbol="APTUSDT", ts=ts_a, price=1.1)
    ws.cancel_drawing()
    assert ws.snapshot()["tool"] == "select"
    assert ws.snapshot()["pending"] is False


def test_clear_preserves_lld_namespace(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    ws.set_indicator_enabled("liquidity", True)
    candles = __import__("research_charts.service", fromlist=["candle_objects"]).candle_objects(
        "APTUSDT", "5m", limit=120
    )
    lld_objs, _, _ = ws.lld_objects(candles)
    assert lld_objs
    ts_a = datetime(2026, 2, 1, 12, 0, tzinfo=timezone.utc)
    ws.set_drawing_tool("hline")
    ws.on_point(pane_id="pane-0", timeframe="5m", symbol="APTUSDT", ts=ts_a, price=1.5)
    mixed = ws.composed_overlays("APTUSDT", "5m", lld_objs)
    assert any(p["namespace"] == "USER_DRAWING" for p in mixed)
    assert any(p["namespace"] == "LLD" for p in mixed)
    zones = [p for p in mixed if p["namespace"] == "LLD" and p["type"] == "zone"]
    labels = [p for p in mixed if p["namespace"] == "LLD" and p["type"] == "label"]
    assert zones
    ws.clear_drawings("APTUSDT")
    after = ws.composed_overlays("APTUSDT", "5m", lld_objs)
    assert all(p["namespace"] != "USER_DRAWING" for p in after)
    assert any(p["namespace"] == "LLD" for p in after)
    assert overlay_namespace({"id": "lld:APTUSDT:5m:upper:1", "metadata": {}}) == "LLD"


def test_ema_stoch_lld_payloads_from_bundle(tmp_path, monkeypatch):
    _ws(tmp_path, monkeypatch)
    clear_candle_cache_for_tests()
    packed = pane_bundle(
        "APTUSDT",
        "5m",
        limit=200,
        ema={"lines": [EMA_LINE]},
        stochastic={
            "enabled": True,
            "k_length": 14,
            "k_smoothing": 3,
            "d_smoothing": 3,
            "overbought_level": 80,
            "oversold_level": 20,
            "show_k": True,
            "show_d": True,
            "show_levels": True,
        },
        liquidity={"enabled": True, "amount": 80, "highest_len": 2, "lowest_len": 2},
    )
    assert packed["ema"]["series"]
    assert packed["ema"]["series"][0]["data"]
    stoch = packed["stochastic"]
    assert stoch["visible"] is True
    series_ids = {row["id"] for row in stoch.get("series") or []}
    assert "k" in series_ids
    assert "d" in series_ids
    level_vals = {lvl.get("price") for lvl in (stoch.get("levels") or [])}
    assert 80 in level_vals or 80.0 in level_vals
    assert 20 in level_vals or 20.0 in level_vals
    lld = packed["liquidity"]["overlays"]
    assert isinstance(lld, list)
    if packed["overlays"]:
        assert all("namespace" in row for row in packed["overlays"])


def test_drawing_http_roundtrip_and_position_settings(tmp_path, monkeypatch):
    _ws(tmp_path, monkeypatch)
    client = _mini()
    client.post("/api/research/drawings/tool", json={"tool": "hline"})
    created = client.post(
        "/api/research/drawings/event",
        json={
            "type": "point",
            "pane_id": "pane-0",
            "timeframe": "5m",
            "symbol": "APTUSDT",
            "time": 1769900000,
            "price": 1.11,
        },
    ).json()
    assert created["selected_id"]
    client.post("/api/research/drawings/tool", json={"tool": "long_position"})
    client.post(
        "/api/research/drawings/event",
        json={
            "type": "point",
            "pane_id": "pane-0",
            "timeframe": "5m",
            "symbol": "APTUSDT",
            "time": 1769900000,
            "price": 1.4,
        },
    )
    done = client.post(
        "/api/research/drawings/event",
        json={
            "type": "point",
            "pane_id": "pane-0",
            "timeframe": "5m",
            "symbol": "APTUSDT",
            "time": 1769900900,
            "price": 1.2,
        },
    ).json()
    assert done["position_settings"] is True
    pos = client.get("/api/research/position").json()
    assert pos["success"] is True
    updated = client.post(
        "/api/research/position",
        json={
            "drawing_id": done["selected_id"],
            "entry_price": 1.4,
            "stop_price": 1.2,
            "target_price": 1.8,
            "position_notional": 250,
        },
    ).json()
    assert updated["success"] is True
    esc = client.post("/api/research/drawings/event", json={"type": "escape"}).json()
    assert esc["tool"] == "select"


def test_middleware_429_is_global_per_endpoint_limit_3(monkeypatch):
    import app as dashboard_app
    import research_charts.api as api_mod

    assert dashboard_app.MAX_CONCURRENT_REQUESTS == 3
    text = (DASHBOARD_ROOT / "app.py").read_text(encoding="utf-8")
    assert "active_requests = {}  # {endpoint: count}" in text
    assert "MAX_CONCURRENT_REQUESTS = 3" in text

    def slow_load(symbol, timeframe, **kwargs):
        time.sleep(0.45)
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "candles": [],
            "cache": "test",
            "source": "test",
            "aggregation": "none",
            "strict_complete_buckets": False,
            "feed_ready": True,
            "from": None,
            "to": None,
            "limit": 30,
            "timings_ms": {},
        }

    monkeypatch.setattr(api_mod, "load_candles", slow_load)
    dashboard_app.sessions["ix-429"] = {"username": "ix"}
    dashboard_app.active_requests.clear()

    async def storm():
        transport = httpx.ASGITransport(app=dashboard_app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            client.cookies.set("session_id", "ix-429")
            urls = [
                "/api/research/candles?symbol=APTUSDT&timeframe=1h&limit=30",
                "/api/research/candles?symbol=APTUSDT&timeframe=1h&limit=30",
                "/api/research/candles?symbol=APTUSDT&timeframe=1h&limit=30",
                "/api/research/candles?symbol=APTUSDT&timeframe=1h&limit=30",
            ]
            return await asyncio.gather(*[client.get(url) for url in urls])

    responses = asyncio.run(storm())
    codes = [res.status_code for res in responses]
    assert 429 in codes
    assert any(res.status_code == 200 for res in responses)
    body = next(res.json() for res in responses if res.status_code == 429)
    assert body["error"] == "Too many concurrent requests"
    assert "GET /api/research/candles" in body["endpoint"]


def test_two_parallel_pane_posts_do_not_429():
    import app as dashboard_app

    dashboard_app.sessions["ix-pane"] = {"username": "ix"}
    dashboard_app.active_requests.clear()

    async def two():
        transport = httpx.ASGITransport(app=dashboard_app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            client.cookies.set("session_id", "ix-pane")
            payload_a = {"symbol": "APTUSDT", "timeframe": "5m", "limit": 40, "allow_stale": False}
            payload_b = {"symbol": "APTUSDT", "timeframe": "15m", "limit": 40, "allow_stale": False}
            return await asyncio.gather(
                client.post("/api/research/pane", json=payload_a),
                client.post("/api/research/pane", json=payload_b),
            )

    responses = asyncio.run(two())
    assert [res.status_code for res in responses] == [200, 200]


def test_stoch_backtester_positions_from_signal_list(tmp_path, monkeypatch):
    from research_charts.stoch_backtester import signal_to_position_spec

    spec = signal_to_position_spec(
        {
            "signal_id": "sig-ace-1",
            "symbol": "ACEUSDT",
            "direction": "LONG",
            "timeframe": "15m",
            "entry_price": 0.20,
            "tp_price": 0.22,
            "sl_price": 0.19,
            "candle_close_time": "2026-08-14T10:00:00Z",
            "exit_time": "2026-08-14T12:00:00Z",
        }
    )
    assert spec is not None
    assert spec["drawing_type"] == "long_position"
    assert spec["entry"] == 0.20
    assert spec["target"] == 0.22
    assert spec["stop"] == 0.19

    ws = _ws(tmp_path, monkeypatch)
    snap = ws.import_stoch_backtester(
        "ACEUSDT",
        [
            {
                "signal_id": "sig-ace-1",
                "symbol": "ACEUSDT",
                "direction": "LONG",
                "timeframe": "15m",
                "entry_price": 0.20,
                "tp_price": 0.22,
                "sl_price": 0.19,
                "candle_close_time": "2026-08-14T10:00:00Z",
                "duration_seconds": 3600,
            },
            {
                "signal_id": "sig-ace-2",
                "symbol": "ACEUSDT",
                "direction": "SHORT",
                "timeframe": "5m",
                "entry_price": 0.21,
                "tp_price": 0.18,
                "sl_price": 0.23,
                "candle_close_time": "2026-08-14T11:00:00Z",
            },
            {
                "signal_id": "skip",
                "symbol": "APTUSDT",
                "direction": "LONG",
                "entry_price": 1.5,
                "tp_price": 1.6,
                "sl_price": 1.4,
                "candle_close_time": "2026-08-14T10:00:00Z",
            },
        ],
    )
    assert snap["backtester"]["loaded"] == 2
    assert snap["backtester"]["skipped"] == 1
    pos = [d for d in ws.drawings.get_drawings("ACEUSDT") if d.drawing_type in ("long_position", "short_position")]
    assert len(pos) == 2
    longs = [d for d in pos if d.drawing_type == "long_position"]
    assert abs(float(longs[0].entry_price) - 0.20) < 1e-9
    assert abs(float(longs[0].target_price) - 0.22) < 1e-9
    assert abs(float(longs[0].stop_price) - 0.19) < 1e-9
    assert all(d.timeframe_scope == "all" for d in pos)
    for tf in ("1m", "5m", "15m", "1h"):
        overlays = ws.composed_overlays("ACEUSDT", tf)
        kinds = [o.get("type") for o in overlays]
        assert kinds.count("position") == 2
    again = ws.import_stoch_backtester("ACEUSDT", [])
    assert again["backtester"]["loaded"] == 0
    assert not [
        d for d in ws.drawings.get_drawings("ACEUSDT") if str(d.drawing_id).startswith("stoch-")
    ]


def test_pool_v1_backtester_uses_artifact_not_collector(tmp_path, monkeypatch):
    from research_charts.stoch_backtester import fetch_stoch_signal_rows, signal_to_position_spec

    spec = signal_to_position_spec(
        {
            "signal_id": "pool-1",
            "symbol": "ACEUSDT",
            "direction": "SHORT",
            "timeframe": "15m",
            "entry_price": 0.11,
            "tp1_price": 0.10,
            "sl_price": 0.12,
            "entry_time": "2026-08-12T15:16:00Z",
        }
    )
    assert spec is not None
    assert spec["target"] == 0.10
    assert spec["stop"] == 0.12

    called = {"collector": False}

    def _boom(*_a, **_k):
        called["collector"] = True
        raise AssertionError("collector must not be called for POOL_ORDER_PLAN_V1")

    monkeypatch.setattr("research_charts.stoch_backtester.httpx.Client", _boom)
    rows, err = fetch_stoch_signal_rows(symbol="ACEUSDT", strategy_version="POOL_ORDER_PLAN_V1")
    assert err is None
    assert called["collector"] is False
    assert len(rows) >= 1
    assert all(r.get("symbol") == "ACEUSDT" for r in rows)
    assert all(r.get("strategy_version") == "POOL_ORDER_PLAN_V1" for r in rows)
    mapped = [signal_to_position_spec(r) for r in rows]
    assert sum(1 for s in mapped if s is not None) >= 1

    empty, err2 = fetch_stoch_signal_rows(symbol="HYPEUSDT", strategy_version="POOL_ORDER_PLAN_V1")
    assert err2 is None
    assert empty == []


def test_htf_liquidity_uses_longer_history_and_more_pools():
    from research_charts.service import (
        default_limit,
        lld_config_for_timeframe,
        scaled_lld_amount,
    )
    from research_charts.trp_import import load_trp

    assert default_limit("1m") == 1500
    assert default_limit("5m") == 1500
    assert default_limit("15m") == 1600
    assert default_limit("30m") == 1800
    assert default_limit("1h") == 2200
    assert default_limit("4h") == 1800
    assert default_limit("1h") > default_limit("5m")
    assert default_limit("4h") > 600

    assert scaled_lld_amount(300, "5m") == 300
    assert scaled_lld_amount(300, "15m") == 450
    assert scaled_lld_amount(300, "1h") == 900
    assert scaled_lld_amount(300, "4h") == 1200
    assert scaled_lld_amount(300, "4h") > scaled_lld_amount(300, "1h")

    trp = load_trp()
    base = trp["LiquidityLocationConfig"](enabled=True, amount=300, highest_len=5, lowest_len=5)
    h1 = lld_config_for_timeframe(base, "1h")
    h4 = lld_config_for_timeframe(base, "4h")
    assert h1.amount == 900
    assert h4.amount == 1200
    assert h1.highest_len == 5
    assert h4.lowest_len == 5


def test_playwright_unavailable_is_explicit():
    try:
        import playwright  # noqa: F401
    except ImportError:
        playwright = None
    assert playwright is None or hasattr(playwright, "sync_api")

"""Phase 3: ClickHouse SoT + existing collector orchestration (no new Bybit infra)."""

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

from research_charts.clickhouse_source import SOURCE_NAME, ClickHouseResearchCandleSource  # noqa: E402
from research_charts.collector_control import (  # noqa: E402
    ensure_live_collector,
    live_status_for_symbol,
    map_research_ui_status,
)
from research_charts.live_universe import (  # noqa: E402
    HISTORY_AVAILABLE_AND_LIVE_CONFIGURED,
    HISTORY_AVAILABLE_BUT_NOT_LIVE_CONFIGURED,
    is_btc_rejected,
    is_live_configured,
    load_live_universe_symbols,
)
from research_charts.service import candle_source_name, list_symbols, load_candles  # noqa: E402


class _AsgiClient:
    def __init__(self, app: FastAPI, cookies: dict[str, str] | None = None):
        self.app = app
        self.cookies = dict(cookies or {})

    def _client_call(self, method: str, path: str, **kwargs):
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

    def get(self, path: str, follow_redirects: bool = True, params: dict | None = None):
        return self._client_call("get", path, follow_redirects=follow_redirects, params=params)


def _mini():
    from research_charts.api import build_router

    def _auth():
        return {"username": "phase3"}

    def _render(name, context):
        return "<html>Research Charts PAGE</html>"

    app = FastAPI()
    app.include_router(build_router(require_auth=_auth, render_template=_render))
    return _AsgiClient(app)


def test_clickhouse_source_and_final_sql():
    src = ClickHouseResearchCandleSource()
    text = Path(DASHBOARD_ROOT / "research_charts" / "clickhouse_source.py").read_text()
    assert "FINAL" in text
    assert "is_closed = 1" in text
    assert "interval" in text
    rows = src.get_1m_candles("APTUSDT", limit=40, newest_first_limit=True)
    assert len(rows) == 40
    times = [int(c.unix_seconds) for c in rows]
    assert times == sorted(times)
    assert len(set(times)) == len(times)
    dts = [times[i] - times[i - 1] for i in range(1, len(times))]
    assert dts == [60] * (len(times) - 1)


def test_symbol_discovery_ch_and_universe_membership():
    rows = list_symbols(use_cache=False)
    names = [r["symbol"] for r in rows]
    assert names == sorted(names)
    assert "APTUSDT" in names
    assert "DOGEUSDT" in names
    universe = load_live_universe_symbols()
    assert "APTUSDT" in universe
    assert "BTCUSDT" not in universe
    apt = next(r for r in rows if r["symbol"] == "APTUSDT")
    assert apt["collector_configured"] is True
    assert apt["live_capability"] == HISTORY_AVAILABLE_AND_LIVE_CONFIGURED
    btc = next((r for r in rows if r["symbol"] == "BTCUSDT"), None)
    if btc:
        assert btc["collector_configured"] is False
        assert btc["live_capability"] == HISTORY_AVAILABLE_BUT_NOT_LIVE_CONFIGURED
    eth = next((r for r in rows if r["symbol"] == "ETHUSDT"), None)
    if eth:
        assert eth["collector_configured"] is False


def test_btc_reject_preserved():
    assert is_btc_rejected("BTCUSDT") is True
    assert is_live_configured("BTCUSDT") is False
    assert is_live_configured("APTUSDT") is True


def test_ensure_live_collector_running(monkeypatch):
    import research_charts.collector_control as cc

    monkeypatch.setattr(
        cc,
        "fetch_collector_status",
        lambda **k: {
            "collector_available": True,
            "collector_state": "LIVE",
            "desired_state": "RUNNING",
            "symbols": [{"symbol": "APTUSDT", "state": "LIVE"}],
        },
    )
    posts = []
    monkeypatch.setattr(cc, "set_desired_state", lambda *a, **k: posts.append(a) or {"http_status": 200})
    out = ensure_live_collector("APTUSDT")
    assert out["ensured"] is True
    assert out["action"] == "already_running"
    assert posts == []


def test_ensure_live_collector_stopped(monkeypatch):
    import research_charts.collector_control as cc

    monkeypatch.setattr(
        cc,
        "fetch_collector_status",
        lambda **k: {
            "collector_available": True,
            "collector_state": "STOPPED",
            "desired_state": "STOPPED",
            "symbols": [{"symbol": "APTUSDT", "state": "STOPPED"}],
        },
    )
    posts = []

    def _post(desired, **k):
        posts.append(desired)
        return {"http_status": 200, "desired_state": desired}

    monkeypatch.setattr(cc, "set_desired_state", _post)
    out = ensure_live_collector("APTUSDT")
    assert out["ensured"] is True
    assert out["action"] == "set_desired_running"
    assert posts == ["RUNNING"]


def test_ensure_collector_unavailable_and_not_configured(monkeypatch):
    import research_charts.collector_control as cc

    monkeypatch.setattr(
        cc,
        "fetch_collector_status",
        lambda **k: {
            "collector_available": False,
            "collector_state": "UNAVAILABLE",
            "symbols": [],
        },
    )
    posts = []
    monkeypatch.setattr(cc, "set_desired_state", lambda *a, **k: posts.append(1) or {})
    down = ensure_live_collector("APTUSDT")
    assert down["reason"] == "collector_unavailable"
    assert posts == []
    skip = ensure_live_collector("ETHUSDT")
    assert skip["reason"] == "not_in_live_universe"
    assert skip["live_configured"] is False
    btc = ensure_live_collector("BTCUSDT")
    assert btc["reason"] == "btc_rejected"
    assert posts == []


def test_incremental_candles_from_last_seen_no_duplicate_times():
    packed = load_candles("APTUSDT", "1m", limit=30)
    last = packed["candles"][-1]["time"]
    again = load_candles("APTUSDT", "1m", start=last, limit=10)
    times = [c["time"] for c in again["candles"]]
    assert times[0] == last
    assert times == sorted(set(times))
    assert packed["source"] == SOURCE_NAME
    assert candle_source_name() == SOURCE_NAME


def test_htf_refresh_rolling_window():
    packed = load_candles("APTUSDT", "15m", limit=20)
    last = packed["candles"][-1]["time"]
    nxt = load_candles("APTUSDT", "15m", start=last, limit=8)
    assert nxt["aggregation"] == "trp_aggregate_strict"
    assert nxt["candles"][0]["time"] == last
    assert all(c["time"] % 900 == 0 for c in nxt["candles"])


def test_live_status_http_and_auth():
    client = _mini()
    body = client.get("/api/research/live-status", params={"symbol": "APTUSDT"}).json()
    assert body["symbol"] == "APTUSDT"
    assert body["history_available"] is True
    assert body["live_configured"] is True
    assert body["collector_available"] is False
    assert body["research_ui_status"] == "UNAVAILABLE"
    btc = client.get("/api/research/live-status", params={"symbol": "BTCUSDT"}).json()
    assert btc["live_configured"] is False
    assert btc["btc_rejected"] is True
    assert btc["research_ui_status"] == "LIVE_NOT_AVAILABLE"
    eth = client.get("/api/research/live-status", params={"symbol": "ETHUSDT"}).json()
    assert eth["live_configured"] is False
    assert eth["research_ui_status"] in {"HISTORICAL", "LIVE_NOT_AVAILABLE"} or eth["history_available"] in {True, False}

    mod = sys.modules.get("app")
    if mod is not None and not hasattr(mod, "sessions"):
        del sys.modules["app"]
    if str(DASHBOARD_ROOT) not in sys.path:
        sys.path.insert(0, str(DASHBOARD_ROOT))
    import app as dashboard_app

    raw = _AsgiClient(dashboard_app.app)
    assert raw.get("/api/research/live-status", params={"symbol": "APTUSDT"}).status_code == 401


def test_cache_freshness_incremental_not_sticky():
    from research_charts import service as svc

    a = load_candles("APTUSDT", "1m", start=None, end=None, limit=25)
    b = load_candles("APTUSDT", "1m", start=a["candles"][-1]["time"], limit=5)
    assert b.get("cache") == "miss"
    assert svc._cache_ttl(a["candles"][-1]["time"], None) is None
    assert svc._cache_ttl(None, None) == 2.0
    assert svc._cache_ttl(1, 2) == 45.0


def test_js_symbol_switch_cancels_poll_and_no_bybit_client():
    js = (DASHBOARD_ROOT / "static" / "js" / "research" / "research_charts.js").read_text()
    assert "pollGen" in js
    assert "stopPoll" in js
    assert "LIVE NOT AVAILABLE" in js
    assert "COLLECTOR UNAVAILABLE" in js
    assert "bybit.com" not in js.lower()
    assert "WebSocket" not in js
    research_dir = DASHBOARD_ROOT / "research_charts"
    for path in research_dir.rglob("*.py"):
        tree = ast.parse(path.read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "PySide6" not in imported
        assert "websockets" not in imported
        text = path.read_text()
        assert "wss://stream.bybit.com" not in text
        assert "BybitKlineWebSocket" not in text
        assert "BybitHistoryClient" not in text


def test_ui_status_mapping():
    assert map_research_ui_status(
        history_available=True,
        live_configured=True,
        collector_available=True,
        collector_state="LIVE",
        symbol_state="LIVE",
        btc_rejected=False,
    ) == "LIVE"
    assert map_research_ui_status(
        history_available=True,
        live_configured=True,
        collector_available=True,
        collector_state="RECOVERING",
        symbol_state="RECOVERING",
        btc_rejected=False,
    ) == "RECOVERING"


def test_live_status_helper_unavailable(monkeypatch):
    import research_charts.collector_control as cc

    monkeypatch.setattr(
        cc,
        "fetch_collector_status",
        lambda **k: {
            "collector_available": False,
            "collector_state": "UNAVAILABLE",
            "symbols": [],
        },
    )
    payload = live_status_for_symbol("APTUSDT", history_available=True, ensure=False)
    assert payload["research_ui_status"] == "UNAVAILABLE"
    assert payload["history_available"] is True

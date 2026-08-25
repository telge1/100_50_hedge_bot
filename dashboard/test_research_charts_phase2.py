"""Phase 2: MySQL market_candles history + TRP aggregation/indicators."""

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

from research_charts.data_source import MySQLResearchCandleSource, SOURCE_TF, _as_utc  # noqa: E402
from research_charts.service import compute_indicators, known_symbols, list_symbols, load_candles  # noqa: E402
from research_charts.trp_import import load_trp  # noqa: E402


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

    def post(self, path: str, json: dict | None = None):
        return self._client_call("post", path, json=json)


def _mini():
    from research_charts.api import build_router

    def _auth():
        return {"username": "phase2"}

    def _render(name, context):
        return "<html>Research Charts PAGE</html>"

    app = FastAPI()
    app.include_router(build_router(require_auth=_auth, render_template=_render))
    return _AsgiClient(app)


def test_db_schema_adapter_columns():
    src = MySQLResearchCandleSource()
    with src._connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SHOW COLUMNS FROM market_candles")
            cols = {r["Field"] for r in cur.fetchall()}
    for name in ("symbol", "open_time", "open", "high", "low", "close", "volume", "timeframe", "exchange"):
        assert name in cols
    assert src.source_timeframe == "1m"


def test_symbol_discovery_only_symbols_with_candles():
    rows = list_symbols(use_cache=False)
    names = [r["symbol"] for r in rows]
    assert names == sorted(names)
    assert names
    for row in rows:
        assert row["candle_count"] > 0
        assert row["first_time"] < row["last_time"]
        assert row["timeframe"] == "1m"
        assert "collector_configured" in row


def test_symbol_metadata_utc_not_cest():
    apt = next(r for r in list_symbols(use_cache=False) if r["symbol"] == "APTUSDT")
    first = datetime.fromtimestamp(apt["first_time"], tz=timezone.utc)
    assert first.tzinfo is not None
    assert apt["first_time_iso"].endswith("Z")
    naive = datetime(2022, 10, 19, 2, 48)
    expected = int(datetime(2022, 10, 19, 2, 48, tzinfo=timezone.utc).timestamp())
    assert int(_as_utc(naive).timestamp()) == expected


def test_1m_retrieval_spacing():
    packed = load_candles("APTUSDT", "1m", limit=80)
    rows = packed["candles"]
    assert len(rows) == 80
    assert packed["aggregation"] == "none"
    dts = [rows[i]["time"] - rows[i - 1]["time"] for i in range(1, len(rows))]
    assert dts == [60] * (len(rows) - 1)
    assert all(r["high"] >= r["low"] for r in rows)


def test_htf_aggregation_spacing():
    expect = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}
    for tf, step in expect.items():
        packed = load_candles("APTUSDT", tf, limit=40)
        rows = packed["candles"]
        assert packed["aggregation"] == "trp_aggregate_strict"
        assert packed["strict_complete_buckets"] is True
        assert len(rows) >= 10
        dts = [rows[i]["time"] - rows[i - 1]["time"] for i in range(1, len(rows))]
        assert dts == [step] * (len(rows) - 1)
        assert all(r["time"] % step == 0 for r in rows)


def test_strict_incomplete_bucket_dropped():
    trp = load_trp()
    Candle = trp["Candle"]
    base = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    candles = []
    for i in range(4):
        ts = datetime.fromtimestamp(int(base.timestamp()) + i * 60, tz=timezone.utc)
        candles.append(
            Candle(timestamp=ts, open=1, high=1, low=1, close=1, volume=1, symbol="T", timeframe="1m")
        )
    out = trp["aggregate"](candles, "5m", strict_complete_buckets=True)
    assert out == []
    out_loose = trp["aggregate"](candles, "5m", strict_complete_buckets=False)
    assert len(out_loose) == 1


def test_from_to_and_limit():
    end = list_symbols(use_cache=False)[0]["last_time"]
    start = end - 3600
    packed = load_candles("APTUSDT", "1m", start=start, end=end, limit=2000)
    times = [c["time"] for c in packed["candles"]]
    assert times
    assert times[0] >= start
    assert times[-1] <= end
    limited = load_candles("APTUSDT", "1m", limit=25)
    assert len(limited["candles"]) == 25


def test_invalid_symbol_and_timeframe_http():
    client = _mini()
    bad_sym = client.get("/api/research/candles", params={"symbol": "NOPEUSDT", "timeframe": "5m"})
    assert bad_sym.status_code == 404
    bad_tf = client.get("/api/research/candles", params={"symbol": "APTUSDT", "timeframe": "1M"})
    assert bad_tf.status_code == 400


def test_research_page_real_data_load_and_symbol_switch():
    client = _mini()
    page = client.get("/live-charts/research")
    assert page.status_code == 200
    assert "Research Charts" in page.text
    a = client.get("/api/research/candles", params={"symbol": "APTUSDT", "timeframe": "5m", "limit": 30}).json()
    b = client.get("/api/research/candles", params={"symbol": "DOGEUSDT", "timeframe": "5m", "limit": 30}).json()
    assert a["symbol"] == "APTUSDT"
    assert b["symbol"] == "DOGEUSDT"
    assert a["candles"][0]["close"] != b["candles"][0]["close"] or a["candles"][-1]["time"] != b["candles"][-1]["time"]


def test_tf_switch_and_no_pyside_in_trp_loader():
    one = load_candles("APTUSDT", "1m", limit=20)
    five = load_candles("APTUSDT", "5m", limit=20)
    assert one["timeframe"] == "1m"
    assert five["timeframe"] == "5m"
    tree = ast.parse((DASHBOARD_ROOT / "research_charts" / "trp_import.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "PySide6" not in imported
    assert "app" not in imported


def test_ema_stochastic_liquidity_reuse_and_symbol_scope():
    ema = compute_indicators("APTUSDT", "15m", limit=120, ema={"enabled": True})
    assert ema["ema"]["series"]
    assert ema["compute_in"] == "python"
    stoch = compute_indicators(
        "APTUSDT",
        "15m",
        limit=120,
        stochastic={"enabled": True, "k_length": 14, "k_smoothing": 3, "d_smoothing": 3},
    )
    assert stoch["stochastic"]["visible"] is True
    assert stoch["stochastic"]["price_min"] == 0
    doge = compute_indicators("DOGEUSDT", "15m", limit=80, ema={"enabled": True})
    apt_last = ema["ema"]["series"][0]["data"][-1]["value"]
    doge_last = doge["ema"]["series"][0]["data"][-1]["value"]
    assert apt_last != doge_last
    lld = compute_indicators(
        "APTUSDT",
        "15m",
        limit=200,
        liquidity={"enabled": True, "highest_len": 2, "lowest_len": 2, "amount": 50},
    )
    assert "overlays" in lld["liquidity"]


def test_auth_and_known_symbols_http():
    mod = sys.modules.get("app")
    if mod is not None and not hasattr(mod, "sessions"):
        del sys.modules["app"]
    if str(DASHBOARD_ROOT) not in sys.path:
        sys.path.insert(0, str(DASHBOARD_ROOT))
    import app as dashboard_app

    client = _AsgiClient(dashboard_app.app)
    assert client.get("/api/research/symbols").status_code == 401
    dashboard_app.sessions["p2"] = {"username": "p2"}
    auth = _AsgiClient(dashboard_app.app, cookies={"session_id": "p2"})
    body = auth.get("/api/research/symbols").json()
    assert {s["symbol"] for s in body["symbols"]} == known_symbols()


def test_index_recommendation_no_binary_wrapper():
    src = Path(DASHBOARD_ROOT / "research_charts" / "data_source.py").read_text()
    assert "BINARY" not in src
    assert "timeframe = %s" in src


def test_clickhouse_config_ignores_polluted_process_database(monkeypatch):
    from research_charts.clickhouse_config import load_clickhouse_config

    monkeypatch.setenv("CLICKHOUSE_DATABASE", "orderbook_analysis")
    cfg = load_clickhouse_config()
    assert cfg.database == "signal_generator"

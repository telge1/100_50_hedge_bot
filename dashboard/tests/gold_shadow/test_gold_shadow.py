from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader

from gold_shadow.api import build_router
from gold_shadow.config import load_gold_shadow_db_config
from gold_shadow.queries import SelectOnlyExecutor, assert_select
from gold_shadow.service import build_summary, list_signals, list_slots, list_trades
from tests.gold_shadow.conftest import empty_fetch, fixture_fetch

DASHBOARD = Path(__file__).resolve().parents[2]


def test_assert_select_rejects_writes():
    with pytest.raises(RuntimeError):
        assert_select("INSERT INTO gold_slots VALUES (1)")
    with pytest.raises(RuntimeError):
        assert_select("SELECT 1; DELETE FROM gold_slots")
    assert_select("SELECT COUNT(*) AS n FROM gold_slots")


def test_empty_summary_and_six_free_slots():
    ex = SelectOnlyExecutor(empty_fetch)
    summary = build_summary(ex)
    assert summary["signals_total"] == 0
    assert summary["net_pnl"] == "0"
    assert summary["empty_forward"] is True
    assert summary["unexpected_exchange_activity"] is False
    slots = list_slots(ex)
    assert [s["slot_id"] for s in slots] == [1, 2, 3, 4, 5, 6]
    assert all(s["status"] == "FREE" for s in slots)


def test_fixture_accepted_skips_tp_sl_and_warning():
    ex = SelectOnlyExecutor(fixture_fetch)
    summary = build_summary(ex)
    assert summary["accepted"] == 1
    assert summary["skipped"] == 2
    assert summary["tp"] == 1
    assert summary["sl"] == 1
    assert summary["open_trades"] == 1
    assert summary["unexpected_exchange_activity"] is True
    signals = list_signals(ex)["items"]
    assert {row["timeframe"] for row in signals} == {"4h", "15m"}
    assert {row["decision"] for row in signals} == {"ACCEPTED", "SKIPPED_DUPLICATE"}
    trades = list_trades(ex)["items"]
    assert trades[0]["status"] == "OPEN"
    assert trades[0]["shadow_label"].startswith("SHADOW")
    assert {t["exit_reason"] for t in trades if t["status"] == "CLOSED"} == {"TP", "SL"}
    slots = list_slots(ex)
    slots[0]["status"] = "WEIRD_STATUS"
    assert slots[0]["status"] == "WEIRD_STATUS"


def test_filters_are_bound_parameters():
    seen = []

    def fetch(sql, params):
        seen.append((sql, tuple(params)))
        return empty_fetch(sql, params)

    ex = SelectOnlyExecutor(fetch)
    list_signals(ex, symbol="ETH' OR 1=1", timeframe="15m", decision="SKIPPED", limit=500, offset=-3)
    assert seen
    sql, params = seen[-1]
    assert "%s" in sql
    assert "ETH' OR 1=1" in params
    assert "500" not in sql
    assert params[-2] == 100
    assert params[-1] == 0
    assert "INSERT" not in sql.upper()


def test_blocked_production_name():
    with pytest.raises(RuntimeError):
        load_gold_shadow_db_config(
            {
                "GOLD_SHADOW_DB_HOST": "127.0.0.1",
                "GOLD_SHADOW_DB_USER": "x",
                "GOLD_SHADOW_DB_PASSWORD": "y",
                "GOLD_SHADOW_DB_NAME": "wave_fade_gold_live",
            }
        )


def test_source_has_no_write_calls():
    text = (DASHBOARD / "gold_shadow" / "db.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    called = [node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)]
    assert "commit" not in called
    assert "executemany" not in called


def _client(fetch):
    def auth():
        return {"username": "qa"}

    env = Environment(loader=FileSystemLoader(str(DASHBOARD / "templates")))

    def render(name, ctx):
        return env.get_template(name).render(**ctx)

    app = FastAPI()
    app.include_router(
        build_router(
            require_auth=auth,
            render_template=render,
            executor_factory=lambda: SelectOnlyExecutor(fetch),
        )
    )
    return app


def test_routes_nav_and_offline(monkeypatch):
    import asyncio
    import httpx

    app = _client(empty_fetch)

    async def call(path, follow=True):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path, follow_redirects=follow)

    page = asyncio.run(call("/profit-verlauf/gold-shadow"))
    assert page.status_code == 200
    assert "profit-verlauf-gold-shadow" in (DASHBOARD / "templates" / "gold_shadow.html").read_text()
    assert "/profit-verlauf/gold-shadow" in page.text
    assert "Gold Shadow" in page.text
    assert "Noch keine echten Forward-Signale" in page.text
    assert "gs-badge-FREE" in (DASHBOARD / "static" / "css" / "gold_shadow.css").read_text()
    redir = asyncio.run(call("/gold-shadow", follow=False))
    assert redir.status_code == 302
    assert redir.headers["location"] == "/profit-verlauf/gold-shadow"
    summary = asyncio.run(call("/api/gold-shadow/summary")).json()
    assert summary["empty_forward"] is True
    assert summary["exchange_orders"] == 0
    slots = asyncio.run(call("/api/gold-shadow/slots")).json()["items"]
    assert len(slots) == 6

    def none():
        return None

    offline_app = FastAPI()
    env = Environment(loader=FileSystemLoader(str(DASHBOARD / "templates")))
    offline_app.include_router(
        build_router(
            require_auth=lambda: {"username": "qa"},
            render_template=lambda n, c: env.get_template(n).render(**c),
            executor_factory=none,
        )
    )

    async def off():
        transport = httpx.ASGITransport(app=offline_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/api/gold-shadow/summary")

    res = asyncio.run(off())
    assert res.status_code == 503
    assert res.json()["offline"] is True


def test_fixture_api_and_existing_nav_partial():
    import asyncio
    import httpx

    app = _client(fixture_fetch)

    async def call(path):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)

    warn = asyncio.run(call("/api/gold-shadow/summary")).json()
    assert warn["unexpected_exchange_activity"] is True
    html = (DASHBOARD / "templates" / "partials" / "nav.html").read_text(encoding="utf-8")
    assert 'href="/live-charts/research"' in html
    assert 'href="/profit-verlauf"' in html
    assert "Gold Shadow" in html
    profit = (DASHBOARD / "templates" / "profit_verlauf_2.html").read_text(encoding="utf-8")
    assert "{% set nav_active = 'profit-verlauf' %}" in profit
    research = (DASHBOARD / "templates" / "research_charts.html").read_text(encoding="utf-8")
    assert "{% set nav_active = 'live-charts-research' %}" in research
    assert asyncio.run(call("/api/gold-shadow/signals")).json()["items"][0]["timeframe"] in {"4h", "15m"}

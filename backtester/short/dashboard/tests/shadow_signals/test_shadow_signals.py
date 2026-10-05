from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from jinja2 import Environment, FileSystemLoader

from shadow_signals.api import build_router
from shadow_signals.formatting import (
    DISPLAY_COLUMNS,
    DASHBOARD_TZ,
    format_dashboard_time_label,
    format_endprofit,
    format_pnl_display,
    parse_filter_input_to_utc,
    pnl_css_class,
    pnl_tone,
    row_to_display,
)
from shadow_signals.queries import (
    LONG_VIEW,
    SHORT_VIEW,
    SelectOnlyExecutor,
    assert_select,
    normalize_side,
    table_for_side,
)
from shadow_signals.service import build_shadow_signals_payload

DASHBOARD = Path(__file__).resolve().parents[2]


def test_column_schema_exact():
    assert list(DISPLAY_COLUMNS) == [
        "StartTime",
        "EndTime",
        "Coin",
        "Status",
        "Entry",
        "SL",
        "TP",
        "PnL",
        "Endprofit +/-",
    ]


def test_side_normalization_and_table_whitelist():
    assert normalize_side(None) == "long"
    assert normalize_side("LONG") == "long"
    assert normalize_side("short") == "short"
    assert normalize_side("evil") == "long"
    assert table_for_side("long") == LONG_VIEW
    assert table_for_side("short") == SHORT_VIEW
    assert "live_forward" in LONG_VIEW
    with pytest.raises(RuntimeError):
        assert_select("DELETE FROM x")


def test_allowed_blocked_open_mapping():
    allowed_row = row_to_display(
        {
            "decision_time": datetime(2026, 10, 4, 15, 30, tzinfo=timezone.utc),
            "end_time": datetime(2026, 10, 4, 16, 0, tzinfo=timezone.utc),
            "tracking_status": "CLOSED",
            "symbol": "TUTUSDT",
            "allowed": 1,
            "entry_price": 1.0,
            "initial_sl": 0.9,
            "tp": 1.1,
            "pnl_pct": -1.12,
        }
    )
    assert allowed_row["Status"] == "ALLOWED"
    assert allowed_row["PnL"] == "-1.12%"
    assert allowed_row["Endprofit +/-"] == "-"
    assert allowed_row["pnl_class"] == "profit-negative"

    blocked_row = row_to_display(
        {
            "decision_time": datetime(2026, 10, 4, 15, 45, tzinfo=timezone.utc),
            "end_time": datetime(2026, 10, 4, 16, 0, tzinfo=timezone.utc),
            "tracking_status": "CLOSED",
            "symbol": "TUTUSDT",
            "allowed": 0,
            "entry_price": 1.0,
            "initial_sl": 0.9,
            "tp": 1.1,
            "pnl_pct": -0.83,
        }
    )
    assert blocked_row["Status"] == "BLOCKED"

    open_row = row_to_display(
        {
            "decision_time": datetime(2026, 10, 4, 15, 30, tzinfo=timezone.utc),
            "end_time": None,
            "tracking_status": "OPEN",
            "symbol": "XRPUSDT",
            "allowed": 1,
            "entry_price": 1.0,
            "initial_sl": 0.9,
            "tp": 1.1,
            "pnl_pct": None,
        }
    )
    assert open_row["EndTime"] == "OPEN"
    assert open_row["PnL"] == "OPEN"
    assert open_row["Endprofit +/-"] == "OPEN"
    assert open_row["pnl_class"] == ""


def test_pnl_format_and_colors():
    assert format_pnl_display(1.25, is_open=False) == "+1.25%"
    assert format_pnl_display(-0.83, is_open=False) == "-0.83%"
    assert format_pnl_display(0.0, is_open=False) == "0.00%"
    assert format_pnl_display(None, is_open=True) == "OPEN"
    assert pnl_tone(1.0, is_open=False) == "positive"
    assert pnl_css_class("positive") == "profit-positive"
    assert pnl_css_class("negative") == "profit-negative"
    assert format_endprofit(0.5, is_open=False) == "+"
    assert format_endprofit(-0.1, is_open=False) == "-"
    assert format_endprofit(0.0, is_open=False) == "0"
    assert format_endprofit(None, is_open=True) == "OPEN"


def test_utc_to_dashboard_tz_display():
    utc = datetime(2026, 10, 4, 15, 45, 0, tzinfo=timezone.utc)
    label = format_dashboard_time_label(utc)
    local = utc.astimezone(DASHBOARD_TZ)
    assert label == local.strftime("%d.%m.%y, %H:%M:%S")
    assert "18:45:00" in label


def test_filter_dashboard_tz_to_utc():
    parsed = parse_filter_input_to_utc("2026-10-04T18:45")
    assert parsed is not None
    assert parsed.hour == 15
    assert parsed.minute == 45
    assert parsed.tzinfo == timezone.utc


def _mock_executor():
    calls: list[tuple[str, dict]] = []

    def fetch(sql: str, params: dict):
        calls.append((sql, params))
        if "countIf" in sql:
            return [
                {
                    "total_signals": 2,
                    "closed_signals": 2,
                    "open_signals": 0,
                    "winning_allowed": 0,
                    "allowed_closed_with_pnl": 1,
                    "total_pnl_sum": -1.12,
                }
            ]
        if "count() AS cnt" in sql:
            return [{"cnt": 2}]
        if "ORDER BY" in sql:
            return [
            {
                "decision_time": datetime(2026, 10, 4, 15, 45, tzinfo=timezone.utc),
                "end_time": datetime(2026, 10, 4, 16, 0, tzinfo=timezone.utc),
                "tracking_status": "CLOSED",
                "symbol": "TUTUSDT",
                "allowed": 0,
                "entry_price": 1.0,
                "initial_sl": 0.9,
                "tp": 1.1,
                "pnl_pct": -0.83,
            },
            {
                "decision_time": datetime(2026, 10, 4, 15, 30, tzinfo=timezone.utc),
                "end_time": datetime(2026, 10, 4, 16, 0, tzinfo=timezone.utc),
                "tracking_status": "CLOSED",
                "symbol": "TUTUSDT",
                "allowed": 1,
                "entry_price": 1.0,
                "initial_sl": 0.9,
                "tp": 1.1,
                "pnl_pct": -1.12,
            },
            ]
        return []

    ex = SelectOnlyExecutor(fetch)
    ex._calls = calls  # type: ignore[attr-defined]
    return ex


def test_service_pagination_and_kpi():
    ex = _mock_executor()
    payload = build_shadow_signals_payload(
        ex,
        side="short",
        page=0,
        page_size=50,
    )
    assert payload["side"] == "short"
    assert payload["pagination"]["page_size"] == 50
    assert payload["summary"]["total_signals"] == 2
    assert payload["summary"]["closed_signals"] == 2
    assert payload["summary"]["winning_signals"] == 0
    assert "-1.12" in payload["summary"]["total_pnl"]
    assert len(payload["rows"]) == 2
    assert payload["rows"][0]["StartTime"]
    assert SHORT_VIEW in ex.sql_log[0]


def test_newest_first_query_has_order_by():
    ex = _mock_executor()
    build_shadow_signals_payload(ex, side="long")
    list_sql = [s for s in ex.sql_log if "ORDER BY" in s][0]
    assert "decision_time DESC" in list_sql
    assert LONG_VIEW in list_sql


def test_filter_params_bound():
    ex = _mock_executor()
    build_shadow_signals_payload(
        ex,
        side="long",
        start_time="2026-10-04T18:45",
        end_time="2026-10-04T20:00",
    )
    assert ex.param_log
    assert "start_utc" in ex.param_log[-2] or any("start_utc" in p for p in ex.param_log)


def _client(executor_factory):
    env = Environment(loader=FileSystemLoader(str(DASHBOARD / "templates")))

    def render(name, ctx):
        return env.get_template(name).render(**ctx)

    def auth():
        return {"username": "qa"}

    app = FastAPI()
    app.include_router(
        build_router(
            require_auth=auth,
            render_template=render,
            executor_factory=executor_factory,
        )
    )
    return app


def test_route_requires_auth():
    def deny():
        raise HTTPException(status_code=401, detail="Not authenticated")

    env = Environment(loader=FileSystemLoader(str(DASHBOARD / "templates")))
    app = FastAPI()
    app.include_router(
        build_router(
            require_auth=deny,
            render_template=lambda n, c: env.get_template(n).render(**c),
            executor_factory=lambda: _mock_executor(),
        )
    )

    async def call():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/profit-verlauf/shadow-signals")

    res = asyncio.run(call())
    assert res.status_code == 401


def test_routes_authenticated_and_api():
    app = _client(lambda: _mock_executor())

    async def call(path):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)

    page = asyncio.run(call("/profit-verlauf/shadow-signals"))
    assert page.status_code == 200
    assert "Signal-Verlauf" in page.text
    assert "profit-verlauf-shadow-signals" in (DASHBOARD / "templates" / "shadow_signals.html").read_text()

    api = asyncio.run(call("/api/dashboard/shadow-signals?side=short")).json()
    assert api["success"] is True
    assert api["side"] == "short"
    assert api["columns"] == list(DISPLAY_COLUMNS)
    assert "pagination" in api
    assert "summary" in api
    assert "last_updated" in api

    default_api = asyncio.run(call("/api/dashboard/shadow-signals")).json()
    assert default_api["side"] == "long"


def test_ch_offline_clean_failure():
    app = _client(lambda: None)

    async def call(path):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)

    page = asyncio.run(call("/profit-verlauf/shadow-signals"))
    assert page.status_code == 200
    assert "Shadow-Signal-Daten aktuell nicht verfügbar" in page.text
    api = asyncio.run(call("/api/dashboard/shadow-signals"))
    assert api.status_code == 503
    body = api.json()
    assert body["offline"] is True


def test_no_scanner_registry_imports():
    root = DASHBOARD / "shadow_signals"
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "bot.shadow_signal_registry" not in text
        assert "shadow_signal_registry" not in text
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "shadow_signal_registry" not in node.module


def test_nav_link_present():
    html = (DASHBOARD / "templates" / "partials" / "nav.html").read_text(encoding="utf-8")
    assert "/profit-verlauf/shadow-signals" in html
    assert "Signal-Verlauf" in html
    assert "Shadow Signale" not in html
    assert "/profit-verlauf/gold-shadow" not in html
    assert 'href="/dashboard"' not in html
    assert 'href="/position-calculator"' in html
    assert "Position Calculator" in html
    assert "nav-link-position-calculator" in html


def test_profit_verlauf_2_untouched():
    app_src = (DASHBOARD / "app.py").read_text(encoding="utf-8")
    assert '@app.get("/profit-verlauf_2"' in app_src
    assert "profit_verlauf_2.html" in app_src
    profit_tpl = (DASHBOARD / "templates" / "profit_verlauf_2.html").read_text(encoding="utf-8")
    assert "profit_trades.js" in profit_tpl
    assert "shadow_signals" not in profit_tpl


def test_app_includes_shadow_router_only_addition():
    app_src = (DASHBOARD / "app.py").read_text(encoding="utf-8")
    assert "from shadow_signals.api import build_router as _build_shadow_signals_router" in app_src
    assert "_build_shadow_signals_router(require_auth=require_auth, render_template=render_template)" in app_src


def test_api_requires_auth_in_source():
    text = (DASHBOARD / "shadow_signals" / "api.py").read_text(encoding="utf-8")
    assert text.count("Depends(require_auth)") >= 2


@pytest.mark.optional_ch
def test_real_ch_short_smoke():
    from shadow_signals.db import configured_executor

    ex = configured_executor()
    if ex is None:
        pytest.skip("ClickHouse not configured")
    try:
        payload = build_shadow_signals_payload(ex, side="short", page_size=50)
    except Exception as exc:
        pytest.skip(f"ClickHouse unreachable: {exc}")
    rows = payload["rows"]
    tut = [r for r in rows if "TUT" in r.get("Coin", "")]
    if not tut:
        pytest.skip("No TUT rows in CH short view")
    assert payload["pagination"]["page"] == 0

"""BT-D86B: BT-D85 must not fall through to legacy stoch /load (503)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_html_cache_bust_bt_d86b() -> None:
    html = open("templates/research_charts.html", encoding="utf-8").read()
    assert "phase6xt-bt-d95-three-month-overlay-v1" in html


def test_js_stoch_fallback_bt_d85_guard() -> None:
    js = open("static/js/research/research_charts.js", encoding="utf-8").read()
    idx = js.find("const strategy = lastStochStrategy();")
    assert idx > 0
    before = js[:idx]
    assert before.count("isBtFrozenMonthResearchStrategy(btStrategy())") >= 2


def test_api_load_delegates_bt_d85_strategy_id() -> None:
    api = open("research_charts/api.py", encoding="utf-8").read()
    assert "bt_d85_july_ema_v3_no_pause_research" in api
    assert "api_research_backtester_bt_d85_july_load" in api
    load_idx = api.find("async def api_research_backtester_load")
    delegate_idx = api.find("api_research_backtester_bt_d85_july_load", load_idx)
    assert load_idx > 0 and delegate_idx > load_idx


def test_legacy_load_routes_bt_d85_strategy_id() -> None:
    dash = Path(__file__).resolve().parent
    env = {
        **os.environ,
        "PYTHONPATH": ":".join([str(dash.parent), str(dash), "/home/telgenbuescher/projects/pools+ob+delta_bot"]),
    }
    code = """
import asyncio
import app as app_mod

async def main():
    fn = None
    for route in app_mod.app.routes:
        if getattr(route, "path", None) == "/api/research/backtester/load" and "POST" in getattr(route, "methods", set()):
            fn = route.endpoint
            break
    assert fn is not None
    snap = await fn(
        user={"username": "test"},
        body={"symbol": "DOGEUSDT", "strategy_id": "bt_d85_july_ema_v3_no_pause_research", "visible": True},
    )
    bt = snap.get("backtester") or {}
    assert bt.get("strategy_id") == "bt_d85_july_ema_v3_no_pause_research"
    assert int(bt.get("loaded") or 0) == 50

asyncio.run(main())
print("ok")
"""
    raw = subprocess.check_output([sys.executable, "-c", code], cwd=str(dash), env=env, text=True)
    assert "ok" in raw

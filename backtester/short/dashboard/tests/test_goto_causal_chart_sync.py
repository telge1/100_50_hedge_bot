"""GO TO causal chart sync: UTC/as-of/window parity + EXP_04 pane."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

DASHBOARD_ROOT = Path(__file__).resolve().parents[1]
JS = DASHBOARD_ROOT / "static" / "js" / "research" / "research_charts.js"
GOTO_JS = DASHBOARD_ROOT / "static" / "js" / "research" / "goto_time.js"
HTML = DASHBOARD_ROOT / "templates" / "research_charts.html"
EXPECTED_POOL = "lld:BTCUSDT:5m:lower:1787740200"
CONTACT = "2026-08-26T11:34:51Z"


def test_node_goto_time_helpers():
    node = subprocess.run(
        ["node", str(DASHBOARD_ROOT / "tests" / "test_goto_time_node.js")],
        cwd=str(DASHBOARD_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert node.returncode == 0, node.stdout + node.stderr
    assert "goto_time_node_tests_ok" in node.stdout


def test_html_loads_goto_module_and_seconds_input():
    html = HTML.read_text(encoding="utf-8")
    assert "goto_time.js" in html
    assert 'id="researchGoTo"' in html
    assert "datetime-local" not in html.split('id="researchGoTo"')[1].split(">")[0]
    assert "11:34:51" in html
    assert 'id="researchGotoSyncHint"' in html


def test_js_goto_sets_asof_and_jump_same_ts():
    js = JS.read_text(encoding="utf-8")
    assert "enterHistoricalReplay(goto_ts_utc, win)" in js
    assert "state.liquidityLocationAsOf = asOfIso" in js
    assert 'sourceAction: "go-to"' in js
    assert "jumpToUnix: goto_ts_utc" in js
    assert 'sourceAction: "go-to-lld-asof"' not in js
    assert "Live pools" in (DASHBOARD_ROOT / "templates" / "research_charts.html").read_text()
    assert "exitHistoricalReplay" in js
    assert "clearLiquidityLocationAsOf" in js


def test_exp04_pane_window_contains_target_and_pool(dashboard_cwd_fix=None):
    import os
    import sys

    os.chdir(DASHBOARD_ROOT)
    if str(DASHBOARD_ROOT) not in sys.path:
        sys.path.insert(0, str(DASHBOARD_ROOT))
    from research_charts.oa_import import ensure_oa_on_path
    from research_charts.service import pane_bundle

    ensure_oa_on_path()
    from orderbook_analyse.liquidity_pool_signal.canonical import parse_as_of_iso
    from orderbook_analyse.liquidity_pool_signal.chart_pool_adapter import export_snapshot
    from orderbook_analyse.liquidity_pool_signal import canonical as canon_mod

    # Mirror JS ±4h window
    as_of = parse_as_of_iso(CONTACT)
    ts = int(as_of.timestamp())
    load_from = ts - 4 * 3600
    load_to = ts + 4 * 3600
    assert load_from <= ts <= load_to

    snap = export_snapshot(
        symbol="BTCUSDT", timeframe="5m", window_start=as_of, as_of=as_of
    )
    pool = next(p for p in snap["active_canonical_pools"] if p["pool_id"] == EXPECTED_POOL)
    assert pool["side"] == "BID"
    assert float(pool["lower"]) == 78475.5
    assert float(pool["upper"]) == 78526.2

    packed = pane_bundle(
        "BTCUSDT",
        "5m",
        start=load_from,
        end=load_to,
        liquidity={"enabled": True},
        liquidity_location_as_of=CONTACT,
        allow_stale=True,
    )
    assert packed.get("liquidity_location_as_of") in (CONTACT, CONTACT.replace("Z", "+00:00"))
    # normalize
    assert str(packed.get("liquidity_location_as_of")).startswith("2026-08-26T11:34:51")
    assert packed.get("canonical_snapshot_sha256") == snap.get("canonical_snapshot_sha256")
    candles = packed.get("candles") or []
    times = [int(c["time"]) for c in candles]
    assert times, "candles required"
    assert min(times) <= ts <= max(times), "target must lie in loaded candle span"
    ovs = (packed.get("liquidity") or {}).get("overlays") or []
    hit = [o for o in ovs if EXPECTED_POOL in str(o.get("id") or o.get("pool_id") or "")]
    assert hit, "EXP_04 overlay missing"
    # Overlay x-span intersects visible window
    ov = hit[0]
    o_start = ov.get("start_timestamp") or ov.get("overlay_start_ts") or ov.get("start")
    o_end = ov.get("end_timestamp") or ov.get("overlay_end_ts") or ov.get("end") or load_to
    if o_start is not None:
        assert int(o_start) <= load_to and int(o_end) >= load_from


def test_invalid_as_of_http_400():
    import asyncio
    import sys

    if str(DASHBOARD_ROOT) not in sys.path:
        sys.path.insert(0, str(DASHBOARD_ROOT))
    from fastapi import FastAPI
    import httpx
    from research_charts.api import build_router

    app = FastAPI()
    app.include_router(build_router(require_auth=lambda: {"username": "t"}, render_template=lambda n, c: "ok"))

    async def post(body):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            return await c.post("/api/research/pane", json=body)

    r = asyncio.run(
        post(
            {
                "symbol": "BTCUSDT",
                "timeframe": "5m",
                "liquidity": {"enabled": True},
                "liquidity_location_as_of": "not-a-timestamp",
            }
        )
    )
    assert r.status_code == 400
    assert "orderbook_analyse" not in r.text

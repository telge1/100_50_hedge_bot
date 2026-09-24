"""GO-TO replay view lock + causal pool projection (EXP_04)."""

from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

DASHBOARD_ROOT = Path(__file__).resolve().parents[1]
JS = DASHBOARD_ROOT / "static" / "js" / "research" / "research_charts.js"
CHART_JS = DASHBOARD_ROOT / "static" / "research_trp" / "chart.js"
EXPECTED_POOL = "lld:BTCUSDT:5m:lower:1787740200"
CONTACT = "2026-08-26T11:34:51Z"
CONTACT_TS = int(datetime.fromisoformat(CONTACT.replace("Z", "+00:00")).timestamp())


def test_node_goto_helpers_still_ok():
    r = subprocess.run(
        ["node", str(DASHBOARD_ROOT / "tests" / "test_goto_time_node.js")],
        cwd=str(DASHBOARD_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_js_replay_state_machine_wiring():
    js = JS.read_text(encoding="utf-8")
    chart = CHART_JS.read_text(encoding="utf-8")
    for needle in (
        "CHART_TIME_REPLAY",
        "enterHistoricalReplay",
        "exitHistoricalReplay",
        "isHistoricalReplay",
        "replayGen",
        "stopPoll()",
        "setReplayViewLock",
        "enforceReplayViewOnAllPanes",
        "tf-change-replay",
    ):
        assert needle in js, f"missing {needle} in research_charts.js"
    for needle in (
        "replayViewLock",
        "setReplayViewLock",
        "enforceReplayViewLock",
        "clearReplayViewLock",
    ):
        assert needle in chart, f"missing {needle} in chart.js"


def test_pool_projection_extends_active_pool_only():
    import sys

    oa_src = Path("/home/telgenbuescher/projects/orderbook_analyse/src")
    if str(oa_src) not in sys.path:
        sys.path.insert(0, str(oa_src))
    from orderbook_analyse.liquidity_pool_signal.canonical import (
        clip_overlays_to_as_of,
        project_overlays_for_replay_window,
    )

    as_of = CONTACT_TS
    render_end = CONTACT_TS + 4 * 3600
    clipped = clip_overlays_to_as_of(
        [
            {
                "id": f"{EXPECTED_POOL}:zone",
                "start_timestamp": as_of - 3600,
                "end_timestamp": as_of,
                "extend_right": False,
                "metadata": {"source": "lld", "pool_id": EXPECTED_POOL},
                "style": {"color": "#228bab"},
            },
            {
                "id": "inactive:zone",
                "start_timestamp": as_of - 7200,
                "end_timestamp": as_of,
                "extend_right": False,
                "metadata": {"source": "lld", "pool_id": "inactive"},
            },
        ],
        as_of,
    )
    projected = project_overlays_for_replay_window(
        clipped,
        as_of_unix=as_of,
        render_end_unix=render_end,
        active_pool_ids={EXPECTED_POOL},
    )
    active = next(o for o in projected if EXPECTED_POOL in str(o.get("id")))
    inactive = next(o for o in projected if o.get("id") == "inactive:zone")
    assert int(active["end_timestamp"]) == render_end
    assert active["metadata"]["projected_after_as_of"] is True
    assert int(active["metadata"]["actual_data_end"]) == as_of
    assert int(inactive["end_timestamp"]) == as_of


def test_exp04_pane_projection_sha_unchanged():
    import sys

    if str(DASHBOARD_ROOT) not in sys.path:
        sys.path.insert(0, str(DASHBOARD_ROOT))
    from research_charts.oa_import import ensure_oa_on_path
    from research_charts.service import pane_bundle

    ensure_oa_on_path()
    from orderbook_analyse.liquidity_pool_signal.chart_pool_adapter import export_snapshot
    from orderbook_analyse.liquidity_pool_signal.canonical import parse_as_of_iso

    as_of = parse_as_of_iso(CONTACT)
    ts = int(as_of.timestamp())
    load_from = ts - 4 * 3600
    load_to = ts + 4 * 3600
    snap = export_snapshot(
        symbol="BTCUSDT", timeframe="5m", window_start=as_of, as_of=as_of
    )
    packed = pane_bundle(
        "BTCUSDT",
        "5m",
        start=load_from,
        end=load_to,
        liquidity={"enabled": True},
        liquidity_location_as_of=CONTACT,
        allow_stale=True,
    )
    assert packed.get("canonical_snapshot_sha256") == snap.get("canonical_snapshot_sha256")
    ovs = (packed.get("liquidity") or {}).get("overlays") or []
    hit = next(o for o in ovs if EXPECTED_POOL in str(o.get("id")))
    assert int(hit["end_timestamp"]) == load_to
    assert hit.get("metadata", {}).get("projected_after_as_of") is True
    pool = next(p for p in snap["active_canonical_pools"] if p["pool_id"] == EXPECTED_POOL)
    assert pool["side"] == "BID"
    assert float(pool["lower"]) == 78475.5
    assert float(pool["upper"]) == 78526.2


def test_stale_replay_gen_documented_in_js():
    js = JS.read_text(encoding="utf-8")
    assert "reqReplayGen !== state.replayGen" in js
    assert "abortInflightPaneLoads" in js


def test_invalid_as_of_still_400():
    import sys

    if str(DASHBOARD_ROOT) not in sys.path:
        sys.path.insert(0, str(DASHBOARD_ROOT))
    from research_charts.api import build_router

    app = FastAPI()
    app.include_router(build_router(require_auth=lambda: {"u": 1}, render_template=lambda n, c: "ok"))

    async def post(body):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post("/api/research/pane", json=body)

    r = asyncio.run(
        post(
            {
                "symbol": "BTCUSDT",
                "timeframe": "5m",
                "liquidity": {"enabled": True},
                "liquidity_location_as_of": "bad-ts",
            }
        )
    )
    assert r.status_code == 400

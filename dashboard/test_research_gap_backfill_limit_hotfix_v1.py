"""Phase 6XT — Research gap-backfill limit hotfix (pollLimitForPaneLag cap 3000)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
HARNESS = Path(
    "/home/telgenbuescher/projects/pools+ob+delta_bot/logs/historical_import/"
    "overnight_2026/phase6xt_research_gap_backfill_limit_hotfix_v1/poll_limit_harness.js"
)


def test_poll_limit_harness():
    proc = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    body = json.loads(proc.stdout)
    assert body["max_is_3000"] is True
    assert body["large_lag_1m"] == 3000
    assert body["small_still_50"] is True


def test_source_cap_and_cache_buster():
    js = (DASH / "static/js/research/research_charts.js").read_text(encoding="utf-8")
    html = (DASH / "templates/research_charts.html").read_text(encoding="utf-8")
    assert "return Math.min(3000, Math.ceil(lag / step) + 20);" in js
    assert "return Math.min(4000, Math.ceil(lag / step) + 20);" not in js
    assert "phase6xt-research-large-array-stack-hotfix-v1" in html


def test_api_accepts_3000_rejects_4000():
    import asyncio

    from httpx import ASGITransport, AsyncClient
    from fastapi import FastAPI

    from research_charts.api import build_router

    app = FastAPI()
    app.include_router(build_router(require_auth=lambda: {"u": "t"}, render_template=lambda *a: ""))

    async def run():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            ok = await c.get(
                "/api/research/candles",
                params={"symbol": "DOGEUSDT", "timeframe": "1m", "from": 1790764500, "limit": 3000},
            )
            bad = await c.get(
                "/api/research/candles",
                params={"symbol": "DOGEUSDT", "timeframe": "1m", "from": 1790764500, "limit": 4000},
            )
            m15 = await c.get(
                "/api/research/candles",
                params={"symbol": "DOGEUSDT", "timeframe": "15m", "from": 1789419900, "limit": 50},
            )
            return ok.status_code, bad.status_code, m15.status_code

    s_ok, s_bad, s_15 = asyncio.run(run())
    assert s_ok == 200
    assert s_bad == 422
    assert s_15 == 200

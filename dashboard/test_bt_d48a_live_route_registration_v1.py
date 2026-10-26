"""BT-D48A: live FastAPI must register BT-D46 chronological routes (stale-process guard)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
ENV = {
    "PYTHONPATH": ":".join(
        [
            str(DASH.parent),
            str(DASH),
            "/home/telgenbuescher/projects/pools+ob+delta_bot",
        ]
    ),
}


def _app_routes():
    os.chdir(DASH)
    code = """
import app
out = []
for r in app.app.routes:
    if hasattr(r, 'path') and 'bt-d46-chronological' in r.path:
        out.append((r.path, sorted(getattr(r, 'methods', None) or [])))
print(repr(out))
"""
    raw = subprocess.check_output([sys.executable, "-c", code], env={**os.environ, **ENV}, text=True)
    return eval(raw.strip())


def test_fastapi_registers_bt_d46_routes():
    routes = _app_routes()
    paths = {p for p, _ in routes}
    assert "/api/research/backtester/bt-d46-chronological/meta" in paths
    assert "/api/research/backtester/bt-d46-chronological/load" in paths
    assert "/api/research/backtester/bt-d46-chronological/sync-viewport" in paths
    load_methods = next(m for p, m in routes if p.endswith("/load"))
    assert "POST" in load_methods


@pytest.mark.skipif(not Path("/tmp/dashboard_app_3000.log").exists(), reason="no live log")
def test_live_listener_d46_not_404_when_auth_missing():
    """Running :3000 process must expose route (401/403 OK — 404 means stale import)."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        "http://127.0.0.1:3000/api/research/backtester/bt-d46-chronological/load",
        data=b'{"symbol":"DOGEUSDT"}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        code = 200
    except urllib.error.HTTPError as e:
        code = e.code
    assert code != 404, "stale dashboard process: BT-D46 load route missing (restart app.py on :3000)"

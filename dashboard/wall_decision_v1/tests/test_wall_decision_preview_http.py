"""Contract checks against a running feature-preview dashboard (port 3012).

Skip automatically when the preview is not up. Does not touch live :3000.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pytest

BASE = os.environ.get("WD_PREVIEW_BASE", "http://127.0.0.1:3012")
COOKIE_FILE = os.environ.get("WD_PREVIEW_COOKIE_FILE", "/tmp/wd_v1_preview_session.txt")


def _session_cookie() -> str | None:
    try:
        sid = open(COOKIE_FILE, encoding="utf-8").read().strip()
    except OSError:
        return None
    return f"session_id={sid}" if sid else None


def _get(path: str) -> tuple[int, str]:
    cookie = _session_cookie()
    if not cookie:
        pytest.skip("preview session cookie missing")
    req = urllib.request.Request(
        BASE + path,
        headers={"Cookie": cookie, "Accept": "text/html,application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        pytest.skip(f"preview not reachable: {exc}")


def test_preview_market_profile_is_real_page_not_fixture():
    code, html = _get("/live-charts/market-profile")
    assert code == 200
    assert 'id="mpWallBpTool"' in html
    assert 'id="wdPanel"' in html
    assert "wall_decision_ui.js" in html
    assert "wall_decision_helpers.js" in html
    assert "FIXTURE / REPLAY PREVIEW" not in html
    assert "wall_decision_preview" not in html
    assert "mp-27" in html or "asset_v" in html or "?v=" in html


def test_preview_config_api_marks_non_execution():
    code, body = _get("/api/wall-decision/v1/config")
    assert code == 200
    data = json.loads(body)
    assert data.get("success") is True
    assert data.get("execution") is False
    assert data.get("fixture_route") is False


def test_preview_live_metrics_blocks_invented_ready_inputs():
    cookie = _session_cookie()
    if not cookie:
        pytest.skip("preview session cookie missing")
    payload = json.dumps(
        {
            "symbol": "BTCUSDT",
            "breakpoint": 1,
            "target_wall": None,
            "baseline_qty": None,
            "triggered_at": None,
            "trigger_price": 100.0,
            "live_price": 101.0,
        }
    ).encode()
    req = urllib.request.Request(
        BASE + "/api/wall-decision/v1/live-metrics",
        data=payload,
        headers={
            "Cookie": cookie,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.URLError as exc:
        pytest.skip(f"preview not reachable: {exc}")
    assert data.get("success") is True
    assert data.get("wall_lost") is True
    assert data.get("adapters", {}).get("oi_delta") == "DATA_UNAVAILABLE"
    assert data.get("adapters", {}).get("avr_state") == "DATA_UNAVAILABLE"

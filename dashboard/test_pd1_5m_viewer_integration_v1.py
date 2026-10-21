"""Phase 6XT: 5m PD1 strict viewer integration (read-only CH)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
ART = Path(
    "/home/telgenbuescher/projects/pools+ob+delta_bot/logs/historical_import/"
    "overnight_2026/phase6xt_5m_pd1_strict_viewer_integration_v1"
)
LINE_5M = "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1"
HASH_15M = "4aeb36fe22ef3ab24fc65c8e7bc33b6ca499dd51759d07936c96fee781c641c9"


def _py_env():
    env = {**dict(__import__("os").environ)}
    env["PYTHONPATH"] = ":".join(
        [
            str(DASH.parent),
            str(DASH),
            "/home/telgenbuescher/projects/pools+ob+delta_bot",
            "/home/telgenbuescher/projects/trading_research_platform",
        ]
    )
    env["HISTORICAL_LMV2_BATCH_READER"] = "1"
    return env


def _run_snippet(code: str, timeout: int = 120) -> dict:
    proc = subprocess.run(
        [str(DASH.parent / ".venv/bin/python"), "-c", code],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_py_env(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_capabilities_expose_both_scopes():
    html = (DASH / "templates/research_charts.html").read_text(encoding="utf-8")
    assert 'name="researchExactOriginPoolDetail"' in html
    assert "researchExactOriginPd1Timeframe" not in html
    js = (DASH / "static/js/research/pd1_exact_origin_density_research_v1.js").read_text(
        encoding="utf-8"
    )
    assert "activeScopesForPane" in js
    assert "paneChartTimeframe" in js


@pytest.mark.timeout(120)
def test_15m_default_regression_unchanged():
    body = _run_snippet(
        """
import json, os
os.environ["HISTORICAL_LMV2_BATCH_READER"]="1"
from pd1_landscape_viewer_v1.historical_window_v1 import build_landscape_window
d=build_landscape_window("2026-09-14T21:45:00Z","2026-09-15T05:45:00Z")
prices=d["prices"]
prov=d["provenance"]
print(json.dumps({
  "n": len(prices),
  "p5s": prov.get("phase5s_batch_reader_active"),
  "line": prov.get("pipeline_line_id"),
  "hash": prov.get("config_hash"),
  "tf": prov.get("timeframe"),
}))
"""
    )
    assert body["n"] == 97
    assert body["p5s"] is True
    assert body["line"].endswith("15mpd1_v1")
    assert body["hash"] == HASH_15M
    assert body.get("tf") in (None, "15m")


@pytest.mark.timeout(180)
def test_5m_pd1_pilot_window_readback():
    body = _run_snippet(
        """
import json, os
os.environ["HISTORICAL_LMV2_BATCH_READER"]="1"
from pd1_landscape_viewer_v1.historical_window_v1 import build_landscape_window
d=build_landscape_window(
  "2026-09-16T00:00:00Z",
  "2026-09-17T23:55:00Z",
  timeframe="5m",
  pool_detail=1,
)
prov=d["provenance"]
win=d.get("window") or {}
ctx=win.get("context_from_utc") or prov.get("context_from")
print(json.dumps({
  "n_prices": len(d["prices"]),
  "line": prov.get("pipeline_line_id"),
  "tf": prov.get("timeframe"),
  "slot": prov.get("membership_slot_minutes"),
  "context_from": ctx,
  "pools": len(d.get("pools") or []),
}))
"""
    )
    assert body["line"] == LINE_5M
    assert body["tf"] == "5m"
    assert body["slot"] == 5
    assert body["n_prices"] == 576
    assert body["context_from"] == "2026-09-09T00:00:00Z"


def test_invalid_scope_rejected():
    from pd1_landscape_viewer_v1.historical_window_v1 import LandscapeWindowError, build_landscape_window

    with pytest.raises(LandscapeWindowError) as exc:
        build_landscape_window(
            "2026-09-16T00:00:00Z",
            "2026-09-17T23:55:00Z",
            timeframe="4h",
            pool_detail=1,
        )
    assert exc.value.code == "INVALID_SCOPE"


@pytest.mark.timeout(60)
def test_capabilities_json_artifact():
    ART.mkdir(parents=True, exist_ok=True)
    caps = _run_snippet(
        """
import json
from pd1_landscape_viewer_v1.historical_window_v1 import landscape_capabilities
c=landscape_capabilities()
print(json.dumps({"scopes": c.get("supported_viewer_scopes"), "default_line": c.get("pipeline_line_id")}))
"""
    )
    (ART / "capabilities.json").write_text(json.dumps(caps, indent=2) + "\n", encoding="utf-8")
    assert any(s.get("timeframe") == "5m" for s in caps.get("scopes") or [])

"""Phase 6XT: 5m PD1/PD5/PD10 exact-origin window API parity (read-only CH)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
ART = Path(
    "/home/telgenbuescher/projects/pools+ob+delta_bot/logs/historical_import/"
    "overnight_2026/phase6xt_5m_multi_pd_density_integration_v1"
)
PILOT_FROM = "2026-09-16T00:00:00Z"
PILOT_TO = "2026-09-17T23:55:00Z"
LINES = {
    1: "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1",
    5: "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd5_v1",
    10: "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd10_v1",
}
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


def _run_snippet(code: str, timeout: int = 180) -> dict:
    proc = subprocess.run(
        [str(DASH.parent / ".venv/bin/python"), "-c", code],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_py_env(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("pool_detail", [1, 5, 10])
@pytest.mark.timeout(180)
def test_5m_pd_pilot_window_api_parity(pool_detail: int):
    body = _run_snippet(
        f"""
import json, os
os.environ["HISTORICAL_LMV2_BATCH_READER"]="1"
from pd1_landscape_viewer_v1.historical_window_v1 import build_landscape_window
d=build_landscape_window(
  "{PILOT_FROM}",
  "{PILOT_TO}",
  timeframe="5m",
  pool_detail={pool_detail},
)
prov=d["provenance"]
win=d.get("window") or {{}}
ctx=win.get("context_from_utc") or prov.get("context_from")
print(json.dumps({{
  "n_prices": len(d["prices"]),
  "line": prov.get("pipeline_line_id"),
  "tf": prov.get("timeframe"),
  "slot": prov.get("membership_slot_minutes"),
  "context_from": ctx,
  "pools": len(d.get("pools") or []),
  "pd": {pool_detail},
}}))
"""
    )
    assert body["line"] == LINES[pool_detail]
    assert body["tf"] == "5m"
    assert body["slot"] == 5
    assert body["n_prices"] == 576
    assert body["context_from"] == "2026-09-09T00:00:00Z"
    assert body["pools"] > 0


@pytest.mark.timeout(120)
def test_15m_pd1_regression_unchanged():
    body = _run_snippet(
        """
import json, os
os.environ["HISTORICAL_LMV2_BATCH_READER"]="1"
from pd1_landscape_viewer_v1.historical_window_v1 import build_landscape_window
d=build_landscape_window("2026-09-14T21:45:00Z","2026-09-15T05:45:00Z")
prov=d["provenance"]
print(json.dumps({
  "n": len(d["prices"]),
  "line": prov.get("pipeline_line_id"),
  "hash": prov.get("config_hash"),
  "p5s": prov.get("phase5s_batch_reader_active"),
}))
"""
    )
    assert body["n"] == 97
    assert body["line"].endswith("15mpd1_v1")
    assert body["hash"] == HASH_15M
    assert body["p5s"] is True


def test_js_multi_pd_scope_matrix():
    js = (DASH / "static/js/research/pd1_exact_origin_density_research_v1.js").read_text(encoding="utf-8")
    assert "windowStoreForScope" in js
    assert "phase6xt-exact-origin-canvas-planned-hotfix-v1" in js
    assert '{ timeframe: "5m", pool_detail: 1 }' in js
    assert '{ timeframe: "5m", pool_detail: 5 }' in js
    assert '{ timeframe: "5m", pool_detail: 10 }' in js
    assert "mergedSegments = mergedSegments.concat" in js


def test_write_artifact_summary():
    ART.mkdir(parents=True, exist_ok=True)
    summary = {"pilot_window": PILOT_FROM + ".." + PILOT_TO, "lines": LINES}
    (ART / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

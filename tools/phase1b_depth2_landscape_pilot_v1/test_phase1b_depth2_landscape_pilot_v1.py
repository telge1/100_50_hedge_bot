"""Phase 1B pilot tests — synthetic + optional live SSOT S1."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_SPREAD = (
    ROOT.parent / "spread_recovery_hedge_short_dev" / "dashboard"
)
DASHBOARD = (
    DASHBOARD_SPREAD
    if (DASHBOARD_SPREAD / "pd1_landscape_viewer_v1").is_dir()
    else ROOT / "backtester" / "short" / "dashboard"
)
ANALYZER = Path(__file__).resolve().parent / "analyzer_depth2_landscape.js"
RUN_PILOT = Path(__file__).resolve().parent / "run_pilot.py"


def _node_analyze(payload: dict, as_of: str) -> dict:
    inp = {
        "window_payload": payload,
        "as_of_utc": as_of,
        "slot_minutes": 5,
        "large_gap_pct": 0.5,
    }
    proc = subprocess.run(
        ["node", str(ANALYZER)],
        input=json.dumps(inp),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _synthetic_pools():
    t0 = "2026-09-15T00:00:00Z"
    t1 = "2026-09-16T00:00:00Z"
    seg = [t0, t1]
    return [
        {
            "pool_id": "p1",
            "side": "lower",
            "bottom": 0.08,
            "top": 0.082,
            "origin": "o1",
            "segments": [seg],
        },
        {
            "pool_id": "p2",
            "side": "lower",
            "bottom": 0.081,
            "top": 0.083,
            "origin": "o2",
            "segments": [seg],
        },
        {
            "pool_id": "p3",
            "side": "lower",
            "bottom": 0.084,
            "top": 0.085,
            "origin": "o3",
            "segments": [seg],
        },
        {
            "pool_id": "p4",
            "side": "upper",
            "bottom": 0.086,
            "top": 0.087,
            "origin": "u1",
            "segments": [seg],
        },
        {
            "pool_id": "p5",
            "side": "upper",
            "bottom": 0.0865,
            "top": 0.0875,
            "origin": "u2",
            "segments": [seg],
        },
    ]


def test_depth1_excluded_depth2_present():
    payload = {
        "pools": _synthetic_pools(),
        "prices": [{"t": "2026-09-15T01:45:00Z", "c": 0.083}],
        "window": {"from_utc": "2026-09-15T00:00:00Z", "to_utc": "2026-09-15T02:00:00Z"},
    }
    out = _node_analyze(payload, "2026-09-15T01:45:00Z")
    intervals = out["landscape"]["display_intervals"]
    assert all(iv["depth"] >= 2 for iv in intervals)
    assert any(iv["side"] == "lower" for iv in intervals)
    assert any(iv["side"] == "upper" for iv in intervals)
    depths = {iv["depth"] for iv in intervals}
    assert 1 not in depths


def test_origin_counted_once_per_level():
    payload = {"pools": _synthetic_pools(), "prices": []}
    out = _node_analyze(payload, "2026-09-15T01:45:00Z")
    for iv in out["landscape"]["display_intervals"]:
        origins = iv.get("origins") or []
        assert len(origins) == len(set(origins))
        assert iv["depth"] == len(origins)


def test_overlapping_intervals_no_false_gap():
    payload = {"pools": _synthetic_pools(), "prices": []}
    out = _node_analyze(payload, "2026-09-15T01:45:00Z")
    for side in ("lower", "upper"):
        united = out["gaps"]["united_" + side]
        for i in range(len(united) - 1):
            assert united[i + 1]["bottom_price"] > united[i]["top_price"]


def test_gap_half_percent_large():
    proc = subprocess.run(
        [
            "node",
            "-e",
            """
const a = require('./analyzer_depth2_landscape.js');
const gaps = a.computeGaps(
  [{ bottom_price: 0.09, top_price: 0.1, origins: ['a'] },
   { bottom_price: 0.1005, top_price: 0.11, origins: ['b'] }],
  'lower',
  0.5
);
const g = gaps.gaps[0];
if (!g.large_gap) process.exit(1);
if (g.gap_pct < 0.5) process.exit(2);
""",
        ],
        cwd=str(ANALYZER.parent),
        capture_output=True,
    )
    assert proc.returncode == 0


def test_deterministic_digest():
    payload = {"pools": _synthetic_pools(), "prices": []}
    a = _node_analyze(payload, "2026-09-15T01:45:00Z")
    b = _node_analyze(payload, "2026-09-15T01:45:00Z")
    assert json.dumps(a["landscape"]["display_intervals"], sort_keys=True) == json.dumps(
        b["landscape"]["display_intervals"], sort_keys=True
    )


@pytest.mark.integration
def test_s1_live_ssot_pilot():
    sys.path.insert(0, str(DASHBOARD))
    try:
        from pd1_landscape_viewer_v1.historical_window_v1 import build_landscape_window
    except ImportError:
        pytest.skip("dashboard import missing")
    try:
        window = build_landscape_window(
            "2026-09-12T01:45:00Z",
            "2026-09-15T01:45:00Z",
            timeframe="5m",
            pool_detail=1,
        )
    except Exception as exc:
        pytest.skip(f"CH SSOT unavailable: {exc}")
    out = _node_analyze(window, "2026-09-15T01:45:00Z")
    assert out["landscape"]["parity"]["ok"] is True
    assert out["landscape"]["parity"]["forensic_count"] > 0
    prov = window.get("provenance") or {}
    assert prov.get("pipeline_line_id") == (
        "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1"
    )


@pytest.mark.integration
def test_run_pilot_script():
    proc = subprocess.run(
        [sys.executable, str(RUN_PILOT)],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(ROOT),
    )
    if proc.returncode == 2:
        pytest.fail("pilot parity blocked: " + proc.stdout + proc.stderr)
    if proc.returncode != 0 and "SSOT window load failed" in proc.stderr:
        pytest.skip("live SSOT unavailable for run_pilot")
    assert proc.returncode == 0, proc.stderr

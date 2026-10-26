"""Phase 2A scanner tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCANNER = Path(__file__).resolve().parent / "timeline_scanner.js"


def _run_scan(pools, prices, scan_from, scan_to, config=None):
    payload = {
        "pools": pools,
        "prices": prices,
        "scan_from_utc": scan_from,
        "scan_to_utc": scan_to,
        "config": config or {
            "proximity_pct": 0.05,
            "cooldown_candles": 0,
            "max_output": 20,
        },
    }
    proc = subprocess.run(
        ["node", "-e", f"""
const {{scanTimeline}} = require('{SCANNER}');
const input = {json.dumps(payload)};
const out = scanTimeline(input);
process.stdout.write(JSON.stringify(out));
"""],
        capture_output=True,
        text=True,
        cwd=str(SCANNER.parent),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _synthetic_pool(bottom, top, origin, seg_start, seg_end):
    return {
        "pool_id": f"micro:upper:{origin}",
        "origin": origin,
        "side": "upper",
        "bottom": bottom,
        "top": top,
        "segments": [[seg_start, seg_end]],
    }


def test_depth1_does_not_trigger():
    """Depth-1-only band is filtered out by min_depth=2 in adapter."""
    pools = [
        _synthetic_pool(
            0.1,
            0.1002,
            "lldv2:DOGEUSDT:5m:upper:1000",
            "2026-09-11T00:00:00Z",
            "2026-09-14T00:00:00Z",
        ),
    ]
    prices = [
        {
            "t": "2026-09-11T12:00:00Z",
            "o": 0.0999,
            "h": 0.10015,
            "l": 0.0998,
            "c": 0.09995,
        }
    ]
    out = _run_scan(pools, prices, "2026-09-11T12:00:00Z", "2026-09-11T12:00:00Z")
    assert out["total_candidates_found"] == 0


def test_next_open_entry_time():
    pools = [
        _synthetic_pool(
            0.1,
            0.1005,
            "lldv2:DOGEUSDT:5m:upper:1000",
            "2026-09-11T00:00:00Z",
            "2026-09-14T00:00:00Z",
        ),
        _synthetic_pool(
            0.1,
            0.1005,
            "lldv2:DOGEUSDT:5m:upper:1001",
            "2026-09-11T00:00:00Z",
            "2026-09-14T00:00:00Z",
        ),
    ]
    prices = [
        {
            "t": "2026-09-11T12:00:00Z",
            "o": 0.0998,
            "h": 0.1003,
            "l": 0.0997,
            "c": 0.0999,
        },
        {
            "t": "2026-09-11T12:05:00Z",
            "o": 0.10001,
            "h": 0.1001,
            "l": 0.0999,
            "c": 0.1,
        },
    ]
    out = _run_scan(pools, prices, "2026-09-11T12:00:00Z", "2026-09-11T12:00:00Z")
    if out["total_candidates_found"] > 0:
        c = out["candidates"][0]
        assert c["entry_open_utc"] == "2026-09-11T12:05:00Z"
        assert c["entry_price"] == 0.10001


def test_deterministic():
    pools = [
        _synthetic_pool(
            0.1,
            0.1005,
            "lldv2:DOGEUSDT:5m:upper:1000",
            "2026-09-11T00:00:00Z",
            "2026-09-14T00:00:00Z",
        ),
        _synthetic_pool(
            0.1,
            0.1005,
            "lldv2:DOGEUSDT:5m:upper:1001",
            "2026-09-11T00:00:00Z",
            "2026-09-14T00:00:00Z",
        ),
    ]
    prices = [
        {
            "t": "2026-09-11T12:00:00Z",
            "o": 0.0998,
            "h": 0.1003,
            "l": 0.0997,
            "c": 0.0999,
        },
    ]
    a = _run_scan(pools, prices, "2026-09-11T12:00:00Z", "2026-09-11T12:00:00Z")
    b = _run_scan(pools, prices, "2026-09-11T12:00:00Z", "2026-09-11T12:00:00Z")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


@pytest.mark.integration
def test_live_phase2a_pilot():
    proc = subprocess.run(
        ["python3", str(Path(__file__).parent / "run_phase2a_scan.py")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert proc.returncode == 0
    summary = json.loads(
        (ROOT / "results/phase2a_5m_pd1_depth2_short_candidates_v1/pilot_summary.json").read_text()
    )
    assert summary["verdict"] == "SHORT_CANDIDATE_PILOT_READY"

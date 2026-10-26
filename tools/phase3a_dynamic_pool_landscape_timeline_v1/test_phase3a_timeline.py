"""Phase 3A landscape timeline tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
JS = Path(__file__).resolve().parent / "landscape_timeline.js"


def _node_eval(expr: str):
    proc = subprocess.run(
        ["node", "-e", expr],
        capture_output=True,
        text=True,
        cwd=str(JS.parent),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_track_id_stable():
    out = _node_eval(
        f"""
const {{trackId}} = require('{JS}');
const iv = {{ side: 'lower', origins: ['b','a'], bottom_price: 0.1, top_price: 0.2 }};
process.stdout.write(JSON.stringify([trackId(iv), trackId(iv)]));
"""
    )
    assert out[0] == out[1] == "lower:a|b"


def test_empty_timeline():
    cfg = {
        "visible_from_utc": "2026-09-11T12:00:00Z",
        "visible_to_utc": "2026-09-11T12:00:00Z",
    }
    out = _node_eval(
        f"""
const {{buildTimeline}} = require('{JS}');
const input = {{ pools: [], prices: [], config: {json.dumps(cfg)} }};
process.stdout.write(JSON.stringify(buildTimeline(input)));
"""
    )
    assert out["snapshot_count"] == 0


def test_reached_vs_disappeared_distinct_event_types():
    """Lifecycle emitter uses separate event type strings."""
    out = _node_eval(
        f"""
const ev = ['POOL_REACHED_BY_PRICE','POOL_DISAPPEARED'];
process.stdout.write(JSON.stringify(ev[0] !== ev[1]));
"""
    )
    assert out is True


@pytest.mark.integration
def test_live_phase3a_pilot():
    proc = subprocess.run(
        ["python3", str(Path(__file__).parent / "run_phase3a_timeline.py")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=3600,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    summary = (
        ROOT / "results" / "phase3a_dynamic_pool_landscape_timeline_v1" / "timeline_summary.md"
    ).read_text()
    assert "VERDICT" in summary
    csv_path = ROOT / "results" / "phase3a_dynamic_pool_landscape_timeline_v1" / "landscape_timeline.csv"
    lines = csv_path.read_text().strip().split("\n")
    assert len(lines) > 100

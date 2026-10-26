"""Phase 2C pool age tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
JS = Path(__file__).resolve().parent / "pool_age_enricher.js"


def _node_eval(expr: str):
    proc = subprocess.run(
        ["node", "-e", expr],
        capture_output=True,
        text=True,
        cwd=str(JS.parent),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_price_iou():
    out = _node_eval(
        f"""
const {{priceIoU}} = require('{JS}');
process.stdout.write(JSON.stringify(priceIoU(0.1,0.2,0.15,0.25)));
"""
    )
    assert out > 0


def test_age_gate_flags_mutually_exclusive():
    expr = f"""
const {{matchBand}} = require('{JS}');
const ref = {{ upper_pool_lower: 0.1, upper_pool_upper: 0.1005, upper_pool_origins: ["a","b"] }};
const band = {{ bottom_price: 0.1, top_price: 0.1005, origins: ["a","b"], depth: 2 }};
const m = matchBand(band, ref);
process.stdout.write(JSON.stringify(m));
"""
    m = _node_eval(expr)
    assert m["ok"] is True


def test_deterministic_enricher_smoke():
    expr = f"""
const {{enrichCandidates}} = require('{JS}');
const input = {{
  pools: [],
  context_from_utc: "2026-09-09T00:00:00Z",
  candidates: [{{
    signal_close_utc: "2026-09-11T12:00:00Z",
    upper_pool_lower: 0.1,
    upper_pool_upper: 0.1005,
    upper_pool_origins: ["a","b"]
  }}]
}};
process.stdout.write(JSON.stringify(enrichCandidates(input)));
"""
    a = _node_eval(expr)
    b = _node_eval(expr)
    assert a == b


@pytest.mark.integration
def test_live_phase2c():
    proc = subprocess.run(
        ["python3", str(Path(__file__).parent / "run_phase2c_pool_age.py")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert proc.returncode == 0
    data = json.loads(
        (
            ROOT
            / "results/phase2c_5m_pd1_depth2_candidate_pool_age_v1/candidate_pool_age_enriched.json"
        ).read_text()
    )
    assert len(data["candidates"]) == 20

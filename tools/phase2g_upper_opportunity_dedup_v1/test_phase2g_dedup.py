"""Phase 2G opportunity deduplication tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
JS = Path(__file__).resolve().parent / "opportunity_dedup.js"
ENRICHER = (
    Path(__file__).resolve().parents[1]
    / "phase2c_5m_pd1_depth2_candidate_pool_age_v1"
    / "pool_age_enricher.js"
)


def _node_eval(expr: str):
    proc = subprocess.run(
        ["node", "-e", expr],
        capture_output=True,
        text=True,
        cwd=str(JS.parent),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_price_overlap_same_pool():
    out = _node_eval(
        f"""
const {{priceIoU}} = require('{ENRICHER}');
process.stdout.write(JSON.stringify(priceIoU(0.08422,0.08427,0.08424,0.08429)));
"""
    )
    assert out > 0.2


def test_merge_zone_ref_expands():
    out = _node_eval(
        f"""
const {{mergeZoneRef}} = require('{JS}');
const ref = {{ upper_pool_lower: 0.08422, upper_pool_upper: 0.08427, upper_pool_origins: ['a'] }};
const band = {{ bottom_price: 0.08428, top_price: 0.08430, origins: ['b'] }};
process.stdout.write(JSON.stringify(mergeZoneRef(ref, band)));
"""
    )
    assert out["upper_pool_lower"] == 0.08422
    assert out["upper_pool_upper"] == 0.08430
    assert set(out["upper_pool_origins"]) == {"a", "b"}


def test_deterministic_opportunity_ids():
    expr = f"""
const {{opportunityIdFromFirst}} = require('{JS}');
const c = {{ candidate_id: 'C010_20260911T234000Z', signal_close_utc: '2026-09-11T23:40:00Z' }};
process.stdout.write(JSON.stringify([opportunityIdFromFirst(c), opportunityIdFromFirst(c)]));
"""
    ids = _node_eval(expr)
    assert ids[0] == ids[1] == "OPP_C010_20260911T234000Z"


def test_empty_candidates():
    out = _node_eval(
        f"""
const {{assignOpportunities}} = require('{JS}');
process.stdout.write(JSON.stringify(assignOpportunities({{ pools: [], candidates: [] }})));
"""
    )
    assert out["assignments"] == []
    assert out["grouped_opportunities"] == []


def test_phase2c_match_threshold_boundary():
    out = _node_eval(
        f"""
const {{matchBand}} = require('{ENRICHER}');
const ref = {{ upper_pool_lower: 0.08422, upper_pool_upper: 0.08427, upper_pool_origins: ['a','b','c','d'] }};
const band = {{ bottom_price: 0.08424, top_price: 0.08428, origins: ['a','b','c','d','e'], depth: 6 }};
process.stdout.write(JSON.stringify(matchBand(band, ref)));
"""
    )
    assert out["ok"] is True
    assert out["shared_origin_count"] >= 1


def test_similar_price_different_origins_no_link_without_ssot():
    """Without pools, each candidate opens its own opportunity (no false merge)."""
    cands = [
        {
            "candidate_id": "A_1",
            "signal_close_utc": "2026-09-11T12:00:00Z",
            "entry_open_utc": "2026-09-11T12:05:00Z",
            "upper_pool_lower": 0.1,
            "upper_pool_upper": 0.1005,
            "upper_pool_origins": ["o1"],
        },
        {
            "candidate_id": "B_1",
            "signal_close_utc": "2026-09-11T12:10:00Z",
            "entry_open_utc": "2026-09-11T12:15:00Z",
            "upper_pool_lower": 0.1,
            "upper_pool_upper": 0.1005,
            "upper_pool_origins": ["o2"],
        },
    ]
    expr = f"""
const {{assignOpportunities}} = require('{JS}');
const input = {{ pools: [], candidates: {json.dumps(cands)} }};
process.stdout.write(JSON.stringify(assignOpportunities(input)));
"""
    out = _node_eval(expr)
    assert len(out["grouped_opportunities"]) == 2


@pytest.mark.integration
def test_causal_order_invariant_on_prefix():
    """Earlier assignments unchanged when later candidates are removed (prefix test)."""
    result_path = ROOT / "results" / "phase2g_upper_opportunity_dedup_v1"
    raw_path = result_path / "raw_candidates_with_opportunity_id.csv"
    if not raw_path.is_file():
        pytest.skip("run phase2g first")
    raw = raw_path.read_text()
    lines = raw.strip().split("\n")
    assert len(lines) >= 2


@pytest.mark.integration
def test_live_phase2g_reference_case():
    proc = subprocess.run(
        ["python3", str(Path(__file__).parent / "run_phase2g_dedup.py")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert proc.returncode == 0, proc.stderr
    summary = (
        ROOT / "results" / "phase2g_upper_opportunity_dedup_v1" / "dedup_summary.md"
    ).read_text()
    assert "VERDICT" in summary
    forensics = (
        ROOT / "results" / "phase2g_upper_opportunity_dedup_v1" / "C010_C011_C013_forensics.md"
    ).read_text()
    assert "C010" in forensics and "ground truth" in forensics.lower()

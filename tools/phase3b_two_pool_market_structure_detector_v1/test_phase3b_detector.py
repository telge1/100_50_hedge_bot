"""Phase 3B detector tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = Path(__file__).resolve().parent

from two_pool_detector import (  # noqa: E402
    gap_directly_below_pool,
    origins_disjoint,
)


def test_gap_not_price_distance():
    gaps = [
        {
            "gap_bottom_price": 0.08,
            "gap_top_price": 0.081,
            "gap_pct": 1.25,
        }
    ]
    g = gap_directly_below_pool(0.082, gaps, 0.5)
    assert g is not None
    assert g["gap_top_price"] == 0.081


def test_origins_disjoint():
    assert origins_disjoint(["a", "b"], ["c", "d"]) is True
    assert origins_disjoint(["a", "b"], ["a", "c"]) is False


def test_reached_not_disappeared_strings():
    assert "POOL_REACHED_BY_PRICE" != "POOL_DISAPPEARED"


@pytest.mark.integration
def test_live_phase3b_run():
    proc = subprocess.run(
        ["python3", str(TOOLS / "run_phase3b_detector.py")],
        cwd=str(TOOLS),
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    out = json.loads(proc.stdout.strip().split("\n")[-1])
    assert "verdict" in out
    summary = (
        ROOT / "results" / "phase3b_two_pool_market_structure_detector_v1" / "detection_summary.md"
    )
    assert summary.is_file()

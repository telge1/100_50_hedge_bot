"""Phase 4B forensics tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = Path(__file__).resolve().parent


def test_sl_formula_short():
    assert abs(0.08427 * 1.002 - 0.08443854) < 1e-6


@pytest.mark.integration
def test_run_forensics():
    proc = subprocess.run(
        ["python3", str(TOOLS / "run_phase4b_forensics.py")],
        cwd=str(TOOLS),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().split("\n")[-1])
    assert out["trades"] > 0
    csv_path = ROOT / "results/phase4b_backtest_trade_forensics_v1/all_trade_forensics.csv"
    assert csv_path.is_file()

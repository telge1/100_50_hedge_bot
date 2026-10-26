"""Phase 4A backtest tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = Path(__file__).resolve().parent

from backtest_pilot import (  # noqa: E402
    select_long_tp,
    select_short_tp,
)


def test_short_tp_min_distance():
    intervals = [
        {"side": "lower", "bottom_price": 0.08, "top_price": 0.081, "depth": 2},
        {"side": "lower", "bottom_price": 0.075, "top_price": 0.076, "depth": 3},
    ]
    tp, reason = select_short_tp(0.085, intervals, 0.5)
    assert tp is not None
    assert reason == "VERIFIED"
    assert tp["top"] == 0.081


def test_short_sl_formula():
    upper = 0.1
    sl = upper * 1.002
    assert abs(sl - 0.1002) < 1e-9


def test_long_sl_formula():
    lower = 0.1
    sl = lower * 0.998
    assert abs(sl - 0.0998) < 1e-9


@pytest.mark.integration
def test_live_phase4a():
    proc = subprocess.run(
        ["python3", str(TOOLS / "run_phase4a_backtest.py")],
        cwd=str(TOOLS),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().split("\n")[-1])
    assert "verdict" in out

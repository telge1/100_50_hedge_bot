"""Phase 1C unit tests (no ClickHouse)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from profile_lib import (
    classify_s1_upper_anchor,
    ema_context,
    nearest_upper_depth2,
    next_5m_open,
)

ROOT = Path(__file__).resolve().parents[2]
DEPTH1 = Path(__file__).resolve().parent / "depth1_probe.js"


def test_next_5m_open():
    assert next_5m_open("2026-09-15T01:45:00Z") == "2026-09-15T01:50:00Z"


def test_ema_insufficient():
    out = ema_context([{"t": "2026-09-15T01:45:00Z", "c": 0.1}], "2026-09-15T01:45:00Z")
    assert out["status"] == "NOT_VERIFIED"


def test_ema_sufficient():
    prices = [
        {"t": f"2026-09-01T00:{m:02d}:00Z", "c": 0.08 + m * 0.0001}
        for m in range(0, 60, 5)
    ]
    # extend to 250 bars
    base = prices[:]
    for i in range(250):
        prices.append(
            {
                "t": f"2026-09-02T{(i // 12):02d}:{(i % 12) * 5:02d}:00Z",
                "c": 0.08 + i * 0.00001,
            }
        )
    out = ema_context(prices, prices[-1]["t"])
    assert out["status"] == "VERIFIED"
    assert out["emas"]["ema9"] is not None


def test_s1_class_depth1_only():
    nearest = {"contains_price": [], "nearest_below": {"bottom_price": 0.083, "top_price": 0.0835}}
    d1 = {"contains_upper_depth1": [{"bottom_price": 0.0838, "top_price": 0.0839}]}
    c = classify_s1_upper_anchor(0.08384, nearest, d1)
    assert c["hypothesis"] == "B_depth1_only_at_entry_hint"


def test_nearest_upper():
    ivs = [
        {"side": "upper", "bottom_price": 0.084, "top_price": 0.085},
        {"side": "upper", "bottom_price": 0.086, "top_price": 0.087},
    ]
    n = nearest_upper_depth2(ivs, 0.0855)
    assert n["nearest_below"]["top_price"] == 0.085
    assert n["nearest_above"]["bottom_price"] == 0.086


@pytest.mark.integration
def test_live_phase1c_runner():
    proc = subprocess.run(
        ["python3", str(Path(__file__).parent / "run_phase1c.py")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode in (0, 2)
    summary = json.loads(
        (ROOT / "results/phase1c_depth2_manual_case_parity_v1/phase1c_summary.json").read_text()
    )
    assert "verdict" in summary

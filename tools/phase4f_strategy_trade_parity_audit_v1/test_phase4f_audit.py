"""Phase 4F audit smoke tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS = Path(__file__).resolve().parent


def test_audit_runs():
    proc = subprocess.run(
        ["python3", str(TOOLS / "run_phase4f_audit.py")],
        cwd=str(TOOLS),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip())
    assert out["trades"] == 8
    p = ROOT / "results/phase4f_strategy_trade_parity_audit_v1/trade_rule_parity.csv"
    assert p.is_file()

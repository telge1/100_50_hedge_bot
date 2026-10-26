from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_phase4d_unit_unchanged():
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(ROOT / "tools/phase4d_pool_landscape_backtester_v2/test_phase4d_strict.py"), "-q", "-k", "not integration"],
        cwd=str(ROOT / "tools/phase4d_pool_landscape_backtester_v2"),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout

"""Regression: MarketMemoryState Aug 16/17 replay (Phase 6XT-90D addendum)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[5]
_RUNNER = (
    _REPO
    / "backtester/short/dashboard/research_charts/live_micro_lld_v2_ch_test/historical_json_import/phase6xt_7d_context/phase6xt_90d_persistent_market_memory_v1.py"
)
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6xt_90d_persistent_market_memory_v1"


def test_aug1617_regression_runner():
    subprocess.run([sys.executable, str(_RUNNER)], check=True, cwd=str(_REPO))
    results = json.loads((_OUT / "aug1617_regression_results.json").read_text(encoding="utf-8"))
    assert results["all_pass"], results["checks"]

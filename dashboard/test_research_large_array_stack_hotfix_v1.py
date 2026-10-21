"""Phase 6XT — large candle array min/max hotfix."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
HARNESS = Path(
    "/home/telgenbuescher/projects/pools+ob+delta_bot/logs/historical_import/"
    "overnight_2026/phase6xt_research_large_array_stack_hotfix_v1/min_max_unix_harness.js"
)


def test_min_max_harness_executes():
    proc = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    body = json.loads(proc.stdout)
    assert body["allOk"] is True
    by = {c["name"]: c for c in body["cases"]}
    assert by["A_empty"]["n"] == 0
    assert by["E_129600"]["mm"]["min"] < by["E_129600"]["mm"]["max"]
    assert by["F_200000"]["mm"]["min"] < by["F_200000"]["mm"]["max"]


def test_research_charts_no_apply_on_times():
    js = (DASH / "static/js/research/research_charts.js").read_text(encoding="utf-8")
    assert "function minMaxUnixSeconds" in js
    assert "Math.min.apply(null, times)" not in js
    assert "Math.max.apply(null, times)" not in js
    assert "phase6xt-research-large-array-stack-hotfix-v1" in (
        DASH / "templates/research_charts.html"
    ).read_text(encoding="utf-8")
    # No other min.apply on candle-scale arrays in this file
    assert not re.search(r"Math\.min\.apply\s*\(\s*null\s*,", js)


def test_density_and_gap_regression_suite():
    proc = subprocess.run(
        [
            "python",
            "-m",
            "pytest",
            "test_research_gap_backfill_limit_hotfix_v1.py",
            "test_pd1_density_canvas_recursion_hotfix_v1.py",
            "-q",
        ],
        cwd=str(DASH),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

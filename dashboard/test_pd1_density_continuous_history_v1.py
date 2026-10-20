"""Phase 6XT — continuous exact-origin density window navigation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
HARNESS = Path(
    "/home/telgenbuescher/projects/pools+ob+delta_bot/logs/historical_import/"
    "overnight_2026/phase6xt_density_continuous_history_v1/continuous_history_harness.js"
)


def test_continuous_history_harness():
    proc = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout)
    assert body["version"] == "phase6xt-density-fixed-tile-integration-v1"
    assert body["viewportReplayPriority"] is True
    assert body["zoom96h"]["windows"] >= 2
    assert body["zoom96h"]["maxWindowH"] == 72
    assert body["cacheTouchOk"] is True


def test_density_module_exports_chunk_planner():
    js = (DASH / "static/js/research/pd1_exact_origin_density_research_v1.js").read_text(encoding="utf-8")
    ad = (DASH / "static/js/research/pd1_exact_density_trp_adapter.js").read_text(encoding="utf-8")
    assert "fetchPlannedWindows" in js
    assert "exactOriginWindowStore" in js
    assert "planExactOriginApiWindows" in ad
    assert "!(opts && opts.viewport)" in js

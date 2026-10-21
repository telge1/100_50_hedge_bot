"""Phase 6XT root-cause repair — viewport clip + sweep cap (deterministic harness)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(
    "/home/telgenbuescher/projects/pools+ob+delta_bot/logs/historical_import/"
    "overnight_2026/phase6xt_density_rootcause_repair_v1/repair_harness.js"
)
DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")


def test_repair_harness_viewport_and_performance():
    proc = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout)
    cases = {c["case"]: c["win"] for c in body["clipping"]}
    assert cases["oct_chart_viewport"]["ok"] is False
    assert cases["oct_chart_viewport"]["reason"] == "view_outside_data"
    assert cases["replay_goto_sep"]["ok"] is True
    assert cases["explicit_goto_opts"]["ok"] is True
    perf = body["perf"]
    assert perf["pools"] == 182
    assert perf["rawZonesVisibleSweep"] > 0
    assert perf["apiFromIso"] == "2026-09-14T21:45:00.000Z"
    assert perf["apiToIso"] == "2026-09-15T11:45:00.000Z"
    assert perf["registeredAfterCap"] == 900


def test_repair_source_contains_sweep_and_replay():
    js = (DASH / "static/js/research/pd1_exact_origin_density_research_v1.js").read_text(encoding="utf-8")
    ad = (DASH / "static/js/research/pd1_exact_density_trp_adapter.js").read_text(encoding="utf-8")
    assert "replay_window" in js
    assert "sweepT0Ms" in ad
    assert "resolveHistoricalLmv2ViewportCloseWindow" in js
    host = (DASH / "static/js/research/research_charts.js").read_text(encoding="utf-8")
    assert "decisionContext: liveMicroLldV2DecisionContext" in host

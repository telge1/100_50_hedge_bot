"""Phase 6XT — 48h fixed-tile exact-origin density integration (flag OFF by default)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
ART = Path(
    "/home/telgenbuescher/projects/pools+ob+delta_bot/logs/historical_import/"
    "overnight_2026/phase6xt_density_fixed_tile_integration_v1"
)
HARNESS = ART / "integration_harness.js"


def test_integration_harness():
    proc = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    body = json.loads(proc.stdout)
    assert body["version"] == "phase6xt-density-fixed-tile-integration-v1"
    assert body["no865StepSlidingChunks"] is True
    assert body["cacheKeyStable"] is True
    assert body["slidingMaxStepsPerWindow"] <= 864
    assert body["cacheRegression"]["fixed48h"]["miss"] <= body["cacheRegression"]["sliding"]["miss"]


def test_module_wiring():
    js = (DASH / "static/js/research/pd1_exact_origin_density_research_v1.js").read_text(encoding="utf-8")
    ad = (DASH / "static/js/research/pd1_exact_density_trp_adapter.js").read_text(encoding="utf-8")
    html = (DASH / "templates/research_charts.html").read_text(encoding="utf-8")
    rc = (DASH / "static/js/research/research_charts.js").read_text(encoding="utf-8")
    assert 'VERSION = "phase6xt-density-min-depth-slider-v1"' in js
    assert "phase6xt_eod_fixed_tile_48h_v1" in js
    assert "isFixedTile48hEnabled" in js
    assert "planExactOriginLoadWindows" in ad
    assert "planExactOriginFixedTiles" in ad
    assert "exactOriginCacheKeyForWindow" in ad
    assert "EOD_MAX_API_CHUNK_MS" in ad
    assert "phase6xt-density-min-depth-slider-v1" in html
    assert "phase6xt-density-min-depth-slider-v1" in rc
    assert "drawEodDensity" not in js


def test_sliding_fallback_when_flag_off():
    js = (DASH / "static/js/research/pd1_exact_origin_density_research_v1.js").read_text(encoding="utf-8")
    assert 'localStorage.getItem(EOD_FIXED_TILE_48H_FLAG_KEY) === "1"' in js
    assert 'fixedTile48h ? "fixed48h" : "sliding"' in js

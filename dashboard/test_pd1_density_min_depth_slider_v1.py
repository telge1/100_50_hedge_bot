"""Phase 6XT — Exact Origin min-depth display filter (frontend-only)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

DASH = Path(__file__).resolve().parent
ADAPTER = DASH / "static/js/research/pd1_exact_density_trp_adapter.js"
RESEARCH = DASH / "static/js/research/pd1_exact_origin_density_research_v1.js"
HTML = DASH / "templates/research_charts.html"


def _node_filter_harness():
    script = """
const fs = require('fs');
const vm = require('vm');
const path = process.argv[1];
const root = {};
let code = fs.readFileSync(path, 'utf8');
code = code.replace(
  '})(typeof window !== "undefined" ? window : globalThis);',
  '})(root);'
);
vm.runInNewContext(code, { root: root, console: console });
const fn = root.PD1ExactDensityTrpAdapter.filterSegmentsByMinOrigins;
const segs = [
  { metadata: { depth: 1 }, id: 'a' },
  { metadata: { depth: 3 }, id: 'b' },
  { metadata: { depth: 5 }, id: 'c' },
  { metadata: { depth: 8 }, id: 'd' },
];
const out = {
  all: fn(segs, 1).map((s) => s.id),
  min3: fn(segs, 3).map((s) => s.id),
  min5: fn(segs, 5).map((s) => s.id),
  min8: fn(segs, 8).map((s) => s.id),
  reset: fn(segs, 1).length,
};
process.stdout.write(JSON.stringify(out));
"""
    proc = subprocess.run(
        ["node", "-e", script, str(ADAPTER)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    return json.loads(proc.stdout)


def test_filter_segments_by_min_origins_cases():
    body = _node_filter_harness()
    assert body["all"] == ["a", "b", "c", "d"]
    assert body["min3"] == ["b", "c", "d"]
    assert body["min5"] == ["c", "d"]
    assert body["min8"] == ["d"]
    assert body["reset"] == 4


def test_cache_not_mutated_by_filter():
    script = """
const fs = require('fs');
const vm = require('vm');
const root = {};
let code = fs.readFileSync(process.argv[1], 'utf8');
code = code.replace(
  '})(typeof window !== "undefined" ? window : globalThis);',
  '})(root);'
);
vm.runInNewContext(code, { root: root, console: console });
const fn = root.PD1ExactDensityTrpAdapter.filterSegmentsByMinOrigins;
const segs = [{ metadata: { depth: 2 } }, { metadata: { depth: 5 } }];
const before = segs.length;
fn(segs, 5);
process.stdout.write(String(segs.length));
"""
    proc = subprocess.run(["node", "-e", script, str(ADAPTER)], capture_output=True, text=True)
    assert proc.returncode == 0
    assert proc.stdout.strip() == "2"


def test_ui_and_version_wiring():
    html = HTML.read_text(encoding="utf-8")
    assert "researchExactOriginMinDepth" in html
    assert "Mindest-Origins" in html
    assert "researchExactOriginMinDepthReset" in html
    assert "phase6xt-exact-origin-canvas-planned-hotfix-v1" in html
    js = RESEARCH.read_text(encoding="utf-8")
    assert 'VERSION = "phase6xt-exact-origin-canvas-planned-hotfix-v1"' in js
    assert "filterForDisplay" in js
    assert "exactOriginSegmentCache" in js
    assert "reapplyMinDepthFilterAllPanes" in js
    assert "MIN_ORIGINS_STORAGE_KEY" in js
    ad = ADAPTER.read_text(encoding="utf-8")
    assert "function filterSegmentsByMinOrigins" in ad
    assert "filterSegmentsByMinOrigins: filterSegmentsByMinOrigins" in ad


def test_no_api_on_slider_handler():
    js = RESEARCH.read_text(encoding="utf-8")
    block = js[js.find('depthSlider.addEventListener("input"') : js.find("updateUiChrome();", js.find("depthSlider"))]
    assert "fetch(" not in block
    assert "API_WINDOW" not in block
    assert "reapplyMinDepthFilterAllPanes" in block

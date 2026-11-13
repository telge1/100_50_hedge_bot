"""DOGE-LIVE-P24E — identical viewport must not bump exactOriginLoadSerial."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

DASH = Path(__file__).resolve().parent
EO = DASH / "static/js/research/pd1_exact_origin_density_research_v1.js"
HTML = DASH / "templates/research_charts.html"
HARNESS = DASH / "static/js/research/test_p24e_coalesce_logic_v1.js"


def test_p24e_coalesce_logic_harness():
    proc = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    body = json.loads(proc.stdout)
    assert body["ok"] is True


def test_p24e_wiring_in_exact_origin_js():
    js = EO.read_text(encoding="utf-8")
    assert "pendingRefreshMateriallyChangesInflight" in js
    assert "inflightOpts" in js
    assert "material_change" in js
    assert "gate.pending = null" in js
    assert "_eoSupersedeScheduledKey" in js
    assert "_p24eTestHooks" in js


def test_p24e_cache_bust():
    html = HTML.read_text(encoding="utf-8")
    assert "pd1-p24f-1h-4h-bounds-v1" in html

"""1h fixed48h tile API windows: density parity vs sliding planner on same visible span."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

DASH = Path(__file__).resolve().parent


@pytest.mark.timeout(180)
def test_1h_fixed_tiles_match_sliding_pools_in_overlap():
    js = DASH / "logs" / "_1h_tile_parity.js"
    js.write_text(
        """
const fs = require('fs');
const vm = require('vm');
const path = require('path');
const code = fs.readFileSync('static/js/research/pd1_exact_density_trp_adapter.js','utf8');
const sb = { console, Math, Date, Number, Map, Set, parseInt, parseFloat, JSON };
vm.createContext(sb); vm.runInContext(code, sb);
const AD = sb.PD1ExactDensityTrpAdapter;
const lo = Date.parse('2026-07-01T00:00:00Z');
const hi = Date.parse('2026-11-01T00:00:00Z');
const sk = AD.exactOriginScopeKey('1h', 1);
const h0 = Date.parse('2026-08-24T12:00:00Z');
const h1 = Date.parse('2026-08-28T18:00:00Z');
const prefetch = 36*3600*1000;
const tail = 6*3600*1000;
const STEP = 5*60*1000;
const from = Math.max(lo, Math.floor((Date.parse('2026-08-26T00:00:00Z')-prefetch)/STEP)*STEP);
const to = Math.min(hi, Math.ceil((Date.parse('2026-08-27T00:00:00Z')+tail+prefetch)/STEP)*STEP);
const sliding = AD.planExactOriginLoadWindows(from, to, lo, hi, { mode: 'sliding', scopeKey: sk });
const fixed = AD.planExactOriginLoadWindows(from, to, lo, hi, { mode: 'fixed48h', scopeKey: sk });
function sig(wins) {
  return wins.map(w => [w.fromMs, w.toMs].join('..')).sort();
}
console.log(JSON.stringify({ sliding: sliding.length, fixed: fixed.length, slidingSig: sig(sliding), fixedSig: sig(fixed) }));
""",
        encoding="utf-8",
    )
    proc = subprocess.run(["node", str(js)], cwd=str(DASH), capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout.strip())
    assert body["fixed"] >= 1
    assert body["sliding"] >= 1

    from pd1_landscape_viewer_v1.window_response_cache_v1 import clear_cache_for_tests
    from pd1_landscape_viewer_v1.window_service_v1 import get_landscape_window_response

    def pools_sig(from_utc: str, to_utc: str) -> dict:
        clear_cache_for_tests()
        p = get_landscape_window_response(
            from_utc=from_utc,
            to_utc=to_utc,
            timeframe="1h",
            pool_detail=1,
            response_mode="exact_origin_v1",
        )
        out = {}
        for pl in p.get("pools") or []:
            if pl.get("side") != "lower":
                continue
            out[pl["pool_id"]] = pl.get("segments") or []
        return out

    args = dict(
        from_utc="2026-08-24T00:00:00Z",
        to_utc="2026-08-26T00:00:00Z",
        timeframe="1h",
        pool_detail=1,
        response_mode="exact_origin_v1",
    )
    clear_cache_for_tests()
    cold = get_landscape_window_response(**args)
    warm = get_landscape_window_response(**args)
    assert cold.get("response_cache", {}).get("hit") is False
    assert warm.get("response_cache", {}).get("hit") is True
    assert len(cold.get("pools") or []) == len(warm.get("pools") or [])

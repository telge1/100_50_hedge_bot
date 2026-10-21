"""Phase 6XT: 15m PD5/PD10 viewer scope release (read-only CH, no new density)."""

from __future__ import annotations

import hashlib
import itertools
import json
import subprocess
from pathlib import Path

import pytest

DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
ART = Path(
    "/home/telgenbuescher/projects/pools+ob+delta_bot/logs/historical_import/"
    "overnight_2026/phase6xt_15m_multi_pd_minimal_viewer_release_v1"
)
VIS_FROM = "2026-09-16T00:00:00Z"
VIS_TO = "2026-09-17T23:45:00Z"
CONTEXT_FROM = "2026-09-09T00:00:00Z"
LINES = {
    1: "historical_mixed_frozen_doge_apr_sep_2026_v1_15mpd1_v1",
    5: "historical_mixed_frozen_doge_apr_sep_2026_v1_15mpd5_v1",
    10: "historical_mixed_frozen_doge_apr_sep_2026_v1_15mpd10_v1",
}
HASHES = {
    1: "4aeb36fe22ef3ab24fc65c8e7bc33b6ca499dd51759d07936c96fee781c641c9",
    5: "533e04c7cd0e1c93540ed9466a2a5a2eb626f82b828b9a45d1da234703ebd03c",
    10: "bd9ac4f30e177321b688f5adb0fe2681ebb4966d51701e3b323498bfcd0d06c2",
}
# Original JSON active_state_fingerprint at 2026-09-16T00:00:00Z (readiness audit).
FINGERPRINTS_SEP16 = {
    1: "50e16fc8ac56e3468413379e292b58b548e6108c730d4c419aba00440880e457",
    5: "4ef7e7333f080cba162accb5645225806d94c0a79c6f1adfc219a9ecbad33876",
    10: "ffd67753c8d8fc9b26362aa462f5c264b607e2f3a0f4cc8e00c9c6b8d08b3008",
}
UNCHANGED_SHA256 = {
    # Byte-lock rotated for the touching-merge fix. chart.js and the window
    # builder stay on the previous lock.
    "static/js/research/pd1_exact_density_trp_adapter.js": (
        "101d9a087c0c38e27047a5e9e663906e523e7ff191b9125a3ee4349178a22b43"
    ),
    "static/research_trp/chart.js": (
        "8ba510e87c3194bcc6bcb1bffd8f4a48eadc20c6534a05118ab0e7600a60d886"
    ),
    "pd1_landscape_viewer_v1/historical_window_v1.py": (
        "368900b1e1bd1f440472f1c6124ca4b18f0f7186540640c1ca50d64346a327a2"
    ),
}


def _py_env():
    env = {**dict(__import__("os").environ)}
    env["PYTHONPATH"] = ":".join(
        [
            str(DASH.parent),
            str(DASH),
            "/home/telgenbuescher/projects/pools+ob+delta_bot",
            "/home/telgenbuescher/projects/trading_research_platform",
        ]
    )
    env["HISTORICAL_LMV2_BATCH_READER"] = "1"
    return env


def _run_snippet(code: str, timeout: int = 180) -> dict:
    proc = subprocess.run(
        [str(DASH.parent / ".venv/bin/python"), "-c", code],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_py_env(),
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_release_matrix_and_unchanged_engines():
    js = (DASH / "static/js/research/pd1_exact_origin_density_research_v1.js").read_text(
        encoding="utf-8"
    )
    for pd in (1, 5, 10):
        assert '{ timeframe: "15m", pool_detail: %s }' % pd in js
        assert '{ timeframe: "5m", pool_detail: %s }' % pd in js
    assert "mergedSegments = mergedSegments.concat" in js
    assert "gatherPoolsFromStore" in js
    assert "windowStoreForScope" in js
    assert "phase6xt-exact-origin-canvas-planned-hotfix-v1" in js
    assert '{ timeframe: "4h", pool_detail: 10 }' in js
    pds = (1, 5, 10)
    combos = []
    for r in range(0, len(pds) + 1):
        combos.extend(itertools.combinations(pds, r))
    assert len(combos) == 8
    for rel in UNCHANGED_SHA256:
        digest = hashlib.sha256((DASH / rel).read_bytes()).hexdigest()
        assert digest == UNCHANGED_SHA256[rel], rel
    adapter = (DASH / "static/js/research/pd1_exact_density_trp_adapter.js").read_text(
        encoding="utf-8"
    )
    chart = (DASH / "static/research_trp/chart.js").read_text(encoding="utf-8")
    assert "function poolsToExactOriginZones" in adapter
    assert "function computeExactBandsForSide" in adapter
    assert "last.end_timestamp >= z.start_timestamp" in adapter
    assert "start_timestamp - 300" not in adapter
    assert "function drawEodDensity" in chart


def test_cache_and_tile_identities_and_no_cross_pd_merge():
    proc = subprocess.run(
        [
            "node",
            "-e",
            r"""
const fs = require("fs");
const vm = require("vm");
const root = "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard/static/js/research";
const ctx = { window: {}, console, Date, performance: { now: () => 0 }, localStorage: { getItem() { return null; }, setItem() {} } };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(root + "/pd1_exact_density_trp_adapter.js", "utf8"), ctx);
const AD = ctx.window.PD1ExactDensityTrpAdapter;
const keys = [1, 5, 10].map((pd) => AD.exactOriginScopeKey("15m", pd));
const five = [1, 5, 10].map((pd) => AD.exactOriginScopeKey("5m", pd));
const all = keys.concat(five);
const wins = all.map((k) => AD.exactOriginWindowCacheKey(1, 2, k));
const tiles = all.map((k) => AD.exactOriginFixedTileCacheKey(0, 48, k));
const density = fs.readFileSync(root + "/pd1_exact_origin_density_research_v1.js", "utf8");
const render = density.slice(density.indexOf("scopePlans.forEach"));
console.log(JSON.stringify({
  distinct_scopes: new Set(all).size,
  distinct_windows: new Set(wins).size,
  distinct_tiles: new Set(tiles).size,
  slot15: AD.membershipSlotMinutesForTimeframe("15m"),
  slot5: AD.membershipSlotMinutesForTimeframe("5m"),
  concat_in_render: render.indexOf("mergedSegments.concat") !== -1,
  merge_density_in_render: render.indexOf("mergeDensityPools") !== -1,
}));
""",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout.strip())
    assert body["distinct_scopes"] == 6
    assert body["distinct_windows"] == 6
    assert body["distinct_tiles"] == 6
    assert body["slot15"] == 15
    assert body["slot5"] == 5
    assert body["concat_in_render"] is True
    assert body["merge_density_in_render"] is False


@pytest.mark.timeout(420)
def test_15m_pd_reference_window_lines_and_membership():
    body = _run_snippet(
        f"""
import json, os
os.environ["HISTORICAL_LMV2_BATCH_READER"]="1"
from datetime import datetime
from pd1_landscape_viewer_v1.historical_window_v1 import (
    LandscapeWindowError,
    build_landscape_window,
)
from pd1_landscape_viewer_v1.historical_window_batch_v1 import unique_decision_closes_between
from research_charts.historical_pool_state_v1 import load_historical_pool_state_at_close

def parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

vis = unique_decision_closes_between(parse("{VIS_FROM}"), parse("{VIS_TO}"), slot_minutes=15)
ctx = unique_decision_closes_between(parse("{CONTEXT_FROM}"), parse("{VIS_TO}"), slot_minutes=15)
rows = []
for pd in (1, 5, 10):
    d = build_landscape_window("{VIS_FROM}", "{VIS_TO}", timeframe="15m", pool_detail=pd)
    prov = d["provenance"]
    stats = prov.get("phase5s_batch_stats") or {{}}
    st = load_historical_pool_state_at_close("{VIS_FROM}", timeframe="15m", pool_detail=pd)
    by_id = {{p["pool_id"]: p for p in d["pools"]}}
    matched = 0
    bound_mismatch = 0
    for m in st.active_members:
        win = by_id.get(m["pool_id"])
        if win is None:
            continue
        matched += 1
        if abs(float(win["bottom"]) - float(m["lower_bound"])) > 1e-9:
            bound_mismatch += 1
        if abs(float(win["top"]) - float(m["upper_bound"])) > 1e-9:
            bound_mismatch += 1
    bad_id = sum(1 for p in d["pools"] if ":15m:" not in str(p.get("pool_id")))
    bad_bounds = sum(1 for p in d["pools"] if not (float(p["bottom"]) < float(p["top"])))
    missing_origin = sum(1 for p in d["pools"] if not p.get("origin"))
    rows.append({{
        "pd": pd,
        "n_prices": len(d["prices"]),
        "line": prov.get("pipeline_line_id"),
        "hash": prov.get("config_hash"),
        "slot": prov.get("membership_slot_minutes"),
        "context_from": d["window"].get("context_from_utc"),
        "decision_closes": stats.get("decision_closes"),
        "cache_keys_filled": stats.get("cache_keys_filled"),
        "fingerprint": st.active_state_fingerprint,
        "active_at_open": len(st.active_members),
        "matched_bounds": matched,
        "bound_mismatch": bound_mismatch,
        "bad_id": bad_id,
        "bad_bounds": bad_bounds,
        "missing_origin": missing_origin,
        "pools": len(d["pools"]),
    }})
reg = build_landscape_window("2026-09-14T21:45:00Z", "2026-09-15T05:45:00Z")
blocked = []
for tf, pd in (("15m", 3), ("30m", 1)):
    try:
        build_landscape_window("{VIS_FROM}", "{VIS_TO}", timeframe=tf, pool_detail=pd)
        blocked.append({{"tf": tf, "pd": pd, "code": None}})
    except LandscapeWindowError as exc:
        blocked.append({{"tf": tf, "pd": pd, "code": exc.code}})
print(json.dumps({{
    "visible_grid": len(vis),
    "context_grid": len(ctx),
    "lead_grid": len(ctx) - len(vis),
    "rows": rows,
    "regression_n": len(reg["prices"]),
    "regression_line": reg["provenance"].get("pipeline_line_id"),
    "regression_hash": reg["provenance"].get("config_hash"),
    "blocked": blocked,
}}))
""",
        timeout=400,
    )
    assert body["visible_grid"] == 192
    assert body["lead_grid"] == 672
    assert body["context_grid"] == 864
    assert body["regression_n"] == 97
    assert body["regression_line"] == LINES[1]
    assert body["regression_hash"] == HASHES[1]
    for row in body["rows"]:
        pd = row["pd"]
        assert row["line"] == LINES[pd]
        assert row["hash"] == HASHES[pd]
        assert row["slot"] == 15
        assert row["context_from"] == CONTEXT_FROM
        assert row["n_prices"] == 574
        assert row["n_prices"] != 192
        assert row["decision_closes"] == 864
        assert row["cache_keys_filled"] == 864
        assert row["fingerprint"] == FINGERPRINTS_SEP16[pd]
        assert row["active_at_open"] > 0
        assert row["matched_bounds"] == row["active_at_open"]
        assert row["bound_mismatch"] == 0
        assert row["bad_id"] == 0
        assert row["bad_bounds"] == 0
        assert row["missing_origin"] == 0
        assert row["pools"] > 0
    assert body["blocked"] == [
        {"tf": "15m", "pd": 3, "code": "INVALID_SCOPE"},
        {"tf": "30m", "pd": 1, "code": "INVALID_SCOPE"},
    ]
    ART.mkdir(parents=True, exist_ok=True)
    (ART / "reference_window.json").write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")

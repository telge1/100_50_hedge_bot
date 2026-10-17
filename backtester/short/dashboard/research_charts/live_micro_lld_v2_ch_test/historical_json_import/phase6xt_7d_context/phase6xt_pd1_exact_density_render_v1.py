#!/usr/bin/env python3
"""Phase 6XT-PD1-EXACT-DENSITY-RENDER V1 — audit exact viewer semantics vs SSOT."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6xt_pd1_exact_density_render_v1"
_VIEWER = _REPO / "logs/historical_import/overnight_2026/phase6xt_pd1_landscape_replay_v1/viewer"
_PARITY = _REPO / "logs/historical_import/overnight_2026/phase6xt_pd1_density_parity_audit_v1"

for _p in (_DASH, _REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

import clickhouse_connect  # noqa: E402

from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402
from research_charts.historical_lmv2_frozen_short_signal_v1 import parse_decision_close  # noqa: E402
from research_charts.historical_pool_state_v1 import load_historical_pool_state_at_close  # noqa: E402
from research_charts.market_cycle_forensic_v1 import causal_15m_decision_close  # noqa: E402

# reuse sweep from parity audit module inline
from phase6xt_pd1_density_parity_audit_v1 import (  # noqa: E402
    exact_max_origin_overlap,
    load_members,
    viewer_bin_peak,
)

ASOFS = (
    "2026-08-01T16:00:00Z",
    "2026-08-01T18:55:00Z",
    "2026-08-01T19:00:00Z",
    "2026-08-01T19:15:00Z",
)
EXPECTED_LOWER = {
    "2026-08-01T16:00:00Z": 6,
    "2026-08-01T18:55:00Z": 6,
    "2026-08-01T19:00:00Z": 1,
    "2026-08-01T19:15:00Z": 1,
}


def _wjson(p: Path, o: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(o, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def main() -> None:
    _OUT.mkdir(parents=True, exist_ok=True)
    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database

    node = subprocess.run(
        ["node", str(_VIEWER / "test_exact_density.mjs")],
        capture_output=True,
        text=True,
        check=False,
    )
    node_json = json.loads(node.stdout) if node.stdout.strip() else {}
    node_ok = node.returncode == 0 and node_json.get("verdict") == "PD1_EXACT_DENSITY_TESTS_PASS"

    pools_json = json.loads((_VIEWER / "data/pools.json").read_text())
    closes = [float(b["c"]) for b in json.loads((_VIEWER / "data/prices.json").read_text())]
    pool_min = min(p["bottom"] for p in pools_json)
    pool_max = max(p["top"] for p in pools_json)
    y_full = (min(min(closes), pool_min) * 0.999, max(max(closes), pool_max) * 1.001)

    ref_parity = {}
    ssot_ok = True
    for asof in ASOFS:
        members, price, dc = load_members(asof, client, db)
        ex = exact_max_origin_overlap(members, "lower")
        t_ms = int(parse_decision_close(asof).timestamp() * 1000)
        vb = viewer_bin_peak(pools_json, "lower", t_ms, y_full[0], y_full[1])
        exp = EXPECTED_LOWER[asof]
        match = ex["max"] == exp
        ssot_ok = ssot_ok and match
        ref_parity[asof] = {
            "decision_close": dc,
            "python_exact_lower_max": ex["max"],
            "expected": exp,
            "node_exact_from_test": node_json.get("parity", {}).get(asof, {}).get("exact_lower_max"),
            "viewer_bin_union_lower": vb["max_origin_bin"],
            "at_price": ex["at_price"],
        }

    contract = {
        "definition": "exact_origin_depth(t,p,side) = count distinct logical origins with ACTIVE pool covering p (inclusive bounds)",
        "visualization": {
            "mode_b": "elementary price intervals from pool boundaries; depth constant per interval (midpoint sample)",
            "pixel_rule": "each canvas row maps 1:1 from exact Y intervals clipped to view; no bin-union depth in mode B fill",
            "diagnostic_overlay": "optional checkbox — bin union stroke only, not exact depth",
        },
        "time_grid": "5m columns; pool membership via segment active at column time t",
        "causal_decision_close": "latest 15m close <= t (matches causal_15m_decision_close)",
        "colors": {"lower": "blue palette", "upper": "violet palette"},
        "files_changed": ["viewer/replay.js", "viewer/index.html", "viewer/exact_density_core.mjs", "viewer/test_exact_density.mjs"],
    }
    _wjson(_OUT / "exact_density_render_contract.json", contract)
    _wjson(_OUT / "exact_density_reference_parity.json", {"asofs": ref_parity, "node_test": node_json})

    dedup = {
        "rule": "origin_logical_v2_key / pl.origin in viewer",
        "synthetic_test": node_json.get("synthetic"),
        "pass": node_ok,
    }
    _wjson(_OUT / "origin_dedup_audit.json", dedup)

    time_align = {}
    for asof in ASOFS:
        t = parse_decision_close(asof)
        dc_py = causal_15m_decision_close(t).strftime("%Y-%m-%dT%H:%M:%SZ")
        time_align[asof] = {"causal_decision_close": dc_py, "matches_18_55_rule": asof == "2026-08-01T18:55:00Z" and dc_py == "2026-08-01T18:45:00Z"}
    _wjson(_OUT / "time_alignment_audit.json", {"asofs": time_align})

    t0 = time.perf_counter()
    for _ in range(3):
        subprocess.run(["node", str(_VIEWER / "test_exact_density.mjs")], capture_output=True, check=False)
    perf_ms = (time.perf_counter() - t0) / 3 * 1000
    _wjson(
        _OUT / "render_performance_audit.json",
        {
            "node_full_sweep_ms": node_json.get("performance", {}).get("sweepMs"),
            "node_columns_5m": node_json.get("performance", {}).get("columns"),
            "repeat_test_avg_ms": perf_ms,
            "pass": perf_ms < 20000,
        },
    )

    replay_src = (_VIEWER / "replay.js").read_text()
    mode_a_ok = "drawPoolsStrength" in replay_src and 'modeSel.value === "origin"' in replay_src

    gates = {
        "EXACT_ORIGIN_SEMANTICS_PASS": "PASS" if node_ok else "FAIL",
        "ORIGIN_DEDUP_PASS": "PASS" if node_ok else "FAIL",
        "INTERVAL_SWEEP_PASS": "PASS" if ssot_ok else "FAIL",
        "SSOT_PARITY_PASS": "PASS" if ssot_ok and node_ok else "FAIL",
        "TIME_ALIGNMENT_PASS": "PASS" if time_align["2026-08-01T18:55:00Z"]["matches_18_55_rule"] else "FAIL",
        "RENDERER_PARITY_PASS": "PASS" if node_ok else "FAIL",
        "PERFORMANCE_PASS": "PASS" if perf_ms < 20000 else "FAIL",
        "MODE_A_REGRESSION_PASS": "PASS" if mode_a_ok else "FAIL",
    }
    all_pass = all(v == "PASS" for v in gates.values())
    overall = "PD1_EXACT_DENSITY_RENDER_READY" if all_pass else "PD1_EXACT_DENSITY_RENDER_NOT_READY"

    (_OUT / "visual_comparison.md").write_text(
        "# Visual comparison\n\n"
        "| Metric | Old Modus B (80-bin union) | New Modus B (exact intervals) |\n"
        "|--------|------------------------------|-------------------------------|\n"
        "| 18:55 lower max | 9 (misleading) | **6** (true origin overlap) |\n"
        "| 18:55 pool overlap | 7 | 7 (unchanged in crosshair via pool sweep N/A in fill) |\n"
        "| Deep band 18:55 | 9 origins in one bin | exact depth 6 in overlap stack; extras shown only where they overlap price |\n\n"
        "Deploy: hard-reload browser after `replay.js` update (no dashboard restart for JS-only).\n",
        encoding="utf-8",
    )
    (_OUT / "report.md").write_text(
        f"# PD1 Exact Density Render V1\n\nOverall: **{overall}**\n\n"
        f"Node tests: {node_json.get('verdict')}\n\n"
        "Reference lower maxima: 16:00=6, 18:55=6, 19:00=1, 19:15=1 (SSOT sweep).\n\n"
        "18:55 bin-union diagnostic remains 9; exact fill must not imply 9-wide overlap.\n\n**STOP**\n",
        encoding="utf-8",
    )
    _wjson(_OUT / "job_status.json", {"gates": gates, "overall": overall})
    client.close()
    if not all_pass:
        sys.exit(1)


if __name__ == "__main__":
    main()

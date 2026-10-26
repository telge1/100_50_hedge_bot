#!/usr/bin/env python3
"""Phase 4X-AP — read-only 50-close June batch-0 readback after scope fix."""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results/phase4xap_reader_scope_fix_v1"
TOOL_AO = ROOT / "tools/phase4xao_jun_sep_nohup_rollout_v1"
sys.path.insert(0, str(TOOL_AO))

POOLS = ROOT / "backtester/short/dashboard"
pools_hps = str(POOLS / "research_charts/historical_pool_state_v1.py")

from import_path_5m_pd1 import ensure_5m_pd1_readback_import_path
from run_jun_sep_nohup import build_month_plan, optimized_readback, published_stems_month, _setup

paths_after = ensure_5m_pd1_readback_import_path()
hcfg = _setup()
month = "2026-06"
plan = build_month_plan(month)
stems = [c["stem"] for c in plan["batches"][0]["closes"]]
published = published_stems_month(hcfg, month)
if len(published) != 50:
    raise SystemExit(f"expected 50 published june closes, got {len(published)}")
missing = [s for s in stems if s not in published]
if missing:
    raise SystemExit(f"batch0 stems not all published: {missing[:3]}")

from research_charts.historical_pool_state_v1 import load_historical_pool_state_at_close

spread_hps = inspect.getfile(load_historical_pool_state_at_close)

rows = optimized_readback(hcfg, month, stems)
ok = all(
    r.get("json_ch_ok") and r.get("reader_ok") and r.get("active_ok") and r.get("density_ok") for r in rows
)

OUT.mkdir(parents=True, exist_ok=True)
result = {
    "june_batch_0_readback": "PASS" if ok else "FAIL",
    "stems": len(stems),
    "pools_historical_pool_state_module": pools_hps,
    "spread_historical_pool_state_module": spread_hps,
    "paths_after_fix": paths_after,
    "failures": [r for r in rows if not (r.get("json_ch_ok") and r.get("reader_ok") and r.get("active_ok") and r.get("density_ok"))][:5],
}
(OUT / "june_50_readback_verify.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result, indent=2))
raise SystemExit(0 if ok else 1)

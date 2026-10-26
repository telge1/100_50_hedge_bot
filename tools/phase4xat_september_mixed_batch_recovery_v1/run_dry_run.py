#!/usr/bin/env python3
"""Non-production dry-run: batch 46 import slice + readback (no CH writes)."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AO = ROOT / "tools/phase4xao_jun_sep_nohup_rollout_v1"
RESULTS = ROOT / "results/phase4xat_september_mixed_batch_recovery_v1"
APR_SEP = (
    ROOT
    / "backtester/short/dashboard/research_charts/live_micro_lld_v2_ch_test/doge_apr_sep_2026_v1_history"
)
COMBO = "5m_PD1"
PIPELINE = "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1"


def _setup() -> None:
    for p in (ROOT, AO, ROOT / "backtester/short/dashboard"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    from research_charts.trp_import import ensure_trp_on_path

    ensure_trp_on_path()


def main() -> int:
    _setup()
    os.environ.pop("JUN_SEP_NOHUP_EXECUTE_APPROVED", None)
    os.environ.pop("HISTORICAL_JSON_IMPORT_ALLOW_WRITE", None)
    RESULTS.mkdir(parents=True, exist_ok=True)

    from import_path_5m_pd1 import setup_hcfg
    from mixed_batch_import_v1 import assert_published_stems_integrity, stems_to_import
    from run_jun_sep_nohup import build_month_plan, optimized_readback, published_stems_month
    from readback_gate import gate_ok

    hcfg = setup_hcfg()
    plan = build_month_plan("2026-09")
    b46 = plan["batches"][46]
    stems = [c["stem"] for c in b46["closes"]]
    published = published_stems_month(hcfg, "2026-09")
    to_import = stems_to_import(stems, published)

    report: dict = {
        "batch_index": 46,
        "stem_count": len(stems),
        "to_import_count": len(to_import),
        "to_import_stems": to_import,
        "skip_count": len(stems) - len(to_import),
        "ch_writes": False,
    }

    t0 = time.perf_counter()
    assert_published_stems_integrity(
        catalog_root=APR_SEP,
        month="2026-09",
        combo_folder=COMBO,
        pipeline_line_id=PIPELINE,
        config_hash=hcfg.config_hash,
        published_stems=published,
        batch_stems=stems,
    )
    report["skip_integrity_ms"] = int((time.perf_counter() - t0) * 1000)

    t1 = time.perf_counter()
    rows = optimized_readback(hcfg, "2026-09", stems)
    report["readback_ms"] = int((time.perf_counter() - t1) * 1000)
    report["readback_rows"] = len(rows)
    report["readback_gate_ok"] = gate_ok(rows)
    report["dry_run_ok"] = len(rows) == len(stems) and report["readback_gate_ok"]
    report["verdict"] = (
        "READY_FOR_CONTROLLED_SEPTEMBER_RESUME_APPROVAL"
        if report["dry_run_ok"] and report["to_import_count"] == 0
        else "BLOCKED_READBACK"
        if not report["dry_run_ok"]
        else "DRY_RUN_IMPORT_SLICE_OK"
    )

    out_path = RESULTS / "dry_run_batch46.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["verdict"] in (
        "READY_FOR_CONTROLLED_SEPTEMBER_RESUME_APPROVAL",
        "DRY_RUN_IMPORT_SLICE_OK",
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())

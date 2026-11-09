#!/usr/bin/env python3
"""L1 V3AO — V3AN watcher entry + original V3AK downstream (isolated research runner)."""

from __future__ import annotations

import csv
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROBE = Path(__file__).resolve().parent
V3AO = PROBE / "watcher_integration_v3ao"
V3AK = PROBE / "substantial_entry_gate_v3ak"
V3AN = PROBE / "touch_watcher_research_v3an"
for p in (PROBE, V3AO, V3AK, V3AN):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import run_l1_pilot as l1
from v3ak_context import DEFAULT_SNAPSHOT, FROZEN_SHA256, build_context_from_snapshot
from watcher_config import TouchWatcherConfig
from watcher_integration_v3ao.v3ao_context import DEFAULT_WATCHER_CONFIG, build_v3ao_context_from_snapshot
from watcher_integration_v3ao.v3ao_pipeline import run_pipeline_on_pending, pending_candidates

OUT = V3AO
RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def watcher_variants() -> list[TouchWatcherConfig]:
    return [
        DEFAULT_WATCHER_CONFIG,
        TouchWatcherConfig(
            touch_tolerance_pct=0.03,
            confirmation_mode="A1",
            confirmation_min_gain_pct=0.20,
            watch_window_bars=12,
            preset_id="V3AO_SCENARIO_A_A1",
        ),
        TouchWatcherConfig(
            touch_tolerance_pct=0.03,
            confirmation_mode="B_RECLAIM",
            confirmation_min_gain_pct=0.20,
            watch_window_bars=12,
            reclaim_confirmation_bars=2,
            preset_id="V3AO_SCENARIO_B",
        ),
    ]


def main() -> int:
    l1._bootstrap_import_paths()
    mod = sys.modules[l1.__name__]
    frozen_v3ak = build_context_from_snapshot(DEFAULT_SNAPSHOT, expected_sha256=FROZEN_SHA256)
    lines = [ln for ln in frozen_v3ak["ctx"]["lines_by_id"].values()]

    baseline = run_pipeline_on_pending(
        frozen_v3ak["pending"],
        lines=lines,
        candles=frozen_v3ak["candles"],
        upper_line=frozen_v3ak["upper_line"],
        lines_by_id=frozen_v3ak["lines_by_id"],
        entry_gate_code="E3_SUBSTANTIAL_TP2c",
    )

    comparison: list[dict] = []
    for cfg in watcher_variants():
        ctx = build_v3ao_context_from_snapshot(DEFAULT_SNAPSHOT, cfg, expected_sha256=FROZEN_SHA256)
        result = run_pipeline_on_pending(
            ctx["pending"],
            lines=lines,
            candles=ctx["candles"],
            upper_line=ctx["upper_line"],
            lines_by_id=ctx["lines_by_id"],
            entry_gate_code="E3_SUBSTANTIAL_TP2c",
        )
        ex = result["executed"]
        gross = round(sum(float(t["gross_pnl_pct"]) for t in ex if t.get("gross_pnl_pct")), 4)
        comparison.append(
            {
                "variant": cfg.preset_id,
                "touch_tolerance_pct": cfg.touch_tolerance_pct,
                "confirmation_mode": cfg.confirmation_mode,
                "watch_window_bars": cfg.watch_window_bars,
                "entry_candidates": len(pending_candidates(ctx["pending"])),
                "trades_executed": len(ex),
                "gross_pct_sum": gross,
                "v3ak_baseline_trades": len(baseline["executed"]),
                "v3ak_baseline_gross_pct_sum": round(
                    sum(float(t["gross_pnl_pct"]) for t in baseline["executed"] if t.get("gross_pnl_pct")), 4
                ),
            }
        )

        snap_pending = OUT / f"l1_v3ao_pending_{cfg.preset_id}.json"
        slim = [
            {k: v for k, v in p.items() if not k.startswith("_") or k in ("_entry_idx", "_pick_ok", "_d2_gate_ok")}
            for p in ctx["pending"]
        ]
        snap_pending.write_text(json.dumps(slim, indent=2, default=str), encoding="utf-8")

    manifest = {
        "run_id": RUN_ID,
        "drawings_sha256": frozen_v3ak["drawings_sha256"],
        "integration": "V3AO watcher entry + V3AK downstream",
        "baseline_v3ak_e3": {
            "candidates": len(pending_candidates(frozen_v3ak["pending"])),
            "executed": len(baseline["executed"]),
        },
        "watcher_runs": comparison,
    }
    _write_csv(OUT / "l1_v3ao_vs_v3ak_comparison.csv", comparison)
    (OUT / "l1_v3ao_run_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(OUT / "l1_v3ao_run_manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

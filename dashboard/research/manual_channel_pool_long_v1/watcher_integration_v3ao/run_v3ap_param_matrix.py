#!/usr/bin/env python3
"""L1 V3AP — parametrized watcher entry matrix + funnel (equality gate required)."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROBE = Path(__file__).resolve().parents[1]
V3AP = Path(__file__).resolve().parent
V3AK = PROBE / "substantial_entry_gate_v3ak"
V3AH = PROBE / "realistic_pool_tp_v3ah"
V3W = PROBE / "tp_research_v3w"
V3AB = PROBE / "support_break_exit_v3ab"
V3Y = PROBE / "support_break_exit_v3y"
for p in (PROBE, V3AP, V3AK, V3AH, V3W, V3AB, V3Y, PROBE / "touch_watcher_research_v3an"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import run_l1_pilot as l1
from watcher_config import TouchWatcherConfig
from watcher_integration_v3ao.v3ap_funnel import (
    analyze_v3ak_touch_episodes,
    run_full_funnel,
    v3ak_entry_signals,
)
from watcher_integration_v3ao.v3ap_param_matrix import v3ap_watcher_matrix
from watcher_integration_v3ao.v3ap_shared_context import build_watcher_pending_for_cfg, load_shared_probe_context

OUT = V3AP
RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _assert_equality() -> None:
    rc = subprocess.call(
        [sys.executable, str(V3AP / "run_v3ao_equality_test.py")],
        cwd=str(PROBE),
    )
    if rc != 0:
        raise SystemExit("V3AP aborted: run_v3ao_equality_test.py FAIL")


def _flatten_funnel_row(
    preset_id: str,
    cfg: TouchWatcherConfig | None,
    funnel: dict,
    *,
    variant_family: str,
) -> dict:
    doc = cfg.to_documented_dict() if cfg else {}
    row = {
        "run_id": RUN_ID,
        "variant_family": variant_family,
        "preset_id": preset_id,
        "schema_version": doc.get("schema_version", ""),
        "touch_tolerance_pct": doc.get("touch_tolerance_pct", ""),
        "confirmation_mode": doc.get("confirmation_mode", "V3AK_C1"),
        "confirmation_min_gain_pct": doc.get("confirmation_min_gain_pct", ""),
        "watch_window_bars": doc.get("watch_window_bars", ""),
        "reclaim_confirmation_bars": doc.get("reclaim_confirmation_bars", ""),
        "touch_episodes": funnel["touch_episodes"],
        "entry_signals": funnel["entry_signals"],
        "structure_rejected": funnel.get("structure_rejected", 0),
        "pending_candidates": funnel["pending_candidates"],
        "d2_rejected": funnel["d2_rejected"],
        "cap6_rejected": funnel["cap6_rejected"],
        "e3_rejected": funnel["e3_rejected"],
        "executor_no_trade": funnel["executor_no_trade"],
        "trades_opened": funnel["trades_opened"],
        "exits_tp": funnel["exits_tp"],
        "exits_sl": funnel["exits_sl"],
        "exits_sbe": funnel["exits_sbe"],
        "exits_channel": funnel["exits_channel"],
        "exits_other": funnel["exits_other"],
        "gross_pnl_pct_sum": funnel["gross_pnl_pct_sum"],
        "net_pnl_pct_sum": funnel["net_pnl_pct_sum"],
        "d2_reject_reasons_json": json.dumps(funnel["d2_reject_reasons"], sort_keys=True),
        "cap6_reject_reasons_json": json.dumps(funnel["cap6_reject_reasons"], sort_keys=True),
        "e3_reject_reasons_json": json.dumps(funnel["e3_reject_reasons"], sort_keys=True),
        "executor_block_reasons_json": json.dumps(funnel["executor_block_reasons"], sort_keys=True),
    }
    return row


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


def main() -> int:
    _assert_equality()
    l1._bootstrap_import_paths()
    shared = load_shared_probe_context()
    lines = shared["lines"]

    matrix_rows: list[dict] = []
    all_trades: list[dict] = []
    by_line: dict[str, list[dict]] = defaultdict(list)

    # V3AK baseline
    v3ak_es = {
        "touch_episodes": analyze_v3ak_touch_episodes(shared["ctx"]),
        "entry_signals": v3ak_entry_signals(shared["v3ak_pending_post_d2"]),
        "structure_rejected": sum(
            1 for p in shared["ctx"]["pending"] if p.get("long_trigger_allowed") != "yes"
        ),
    }
    v3ak_funnel = run_full_funnel(
        shared["v3ak_pending_post_d2"],
        lines=lines,
        candles=shared["candles"],
        upper_line=shared["upper_line"],
        lines_by_id=shared["lines_by_id"],
        entry_stats=v3ak_es,
    )
    matrix_rows.append(
        _flatten_funnel_row("V3AK_BASELINE_C1", None, v3ak_funnel, variant_family="V3AK")
    )
    for t in v3ak_funnel["trades"]:
        tr = {**t, "preset_id": "V3AK_BASELINE_C1", "variant_family": "V3AK"}
        all_trades.append(tr)
        by_line[t.get("touch_drawing_id", "")].append(tr)

    configs = v3ap_watcher_matrix()
    for i, cfg in enumerate(configs):
        pending, es = build_watcher_pending_for_cfg(shared, cfg)
        funnel = run_full_funnel(
            pending,
            lines=lines,
            candles=shared["candles"],
            upper_line=shared["upper_line"],
            lines_by_id=shared["lines_by_id"],
            entry_stats=es,
        )
        matrix_rows.append(
            _flatten_funnel_row(cfg.preset_id, cfg, funnel, variant_family="WATCHER")
        )
        for t in funnel["trades"]:
            tr = {
                **t,
                "preset_id": cfg.preset_id,
                "variant_family": "WATCHER",
                "touch_tolerance_pct": cfg.touch_tolerance_pct,
                "confirmation_mode": cfg.confirmation_mode,
                "watch_window_bars": cfg.watch_window_bars,
            }
            all_trades.append(tr)
            by_line[t.get("touch_drawing_id", "")].append(tr)
        if (i + 1) % 12 == 0:
            print(f"V3AP progress: {i + 1}/{len(configs)}")

    _write_csv(OUT / "l1_v3ap_parameter_matrix.csv", matrix_rows)
    _write_csv(OUT / "l1_v3ap_all_executed_trades.csv", all_trades)

    # Per-line rollup
    line_rows: list[dict] = []
    for did, trades in sorted(by_line.items()):
        ex = [t for t in trades if t.get("exit_kind") not in ("NO_TRADE", "", None)]
        line_rows.append(
            {
                "touch_drawing_id": did,
                "trade_rows": len(trades),
                "executed": len(ex),
                "gross_pnl_pct_sum": round(
                    sum(float(t["gross_pnl_pct"]) for t in ex if t.get("gross_pnl_pct")), 4
                ),
            }
        )
    _write_csv(OUT / "l1_v3ap_by_line_summary.csv", line_rows)

    scope = {
        "run_id": RUN_ID,
        "drawings_sha256": shared["drawings_sha256"],
        "symbol": l1.SYMBOL,
        "timeframe": l1.TIMEFRAME,
        "window_start_utc": shared["window_start_utc"],
        "window_end_utc": shared["window_end_utc"],
        "simulation_end_utc": shared["simulation_end_utc"],
        "n_drawings": len(shared["drawing_inventory"]),
        "n_candles": len(shared["candles"]),
        "lookahead_note": (
            "RETROSPECTIVE_DRAWINGS: line anchors from frozen snapshot; "
            "pool SSOT at confirm-bar close only; no future pool injection in V3AO materialize."
        ),
        "equality_gate": "PASS",
        "matrix_configs": len(configs) + 1,
    }
    (OUT / "l1_v3ap_data_scope.json").write_text(json.dumps(scope, indent=2), encoding="utf-8")

    block_csv_rows = []
    for row in matrix_rows:
        for stage in ("d2", "cap6", "e3", "executor"):
            key = f"{stage}_reject_reasons_json"
            if key in row and row.get(f"{stage}_rejected"):
                reasons = json.loads(row[key]) if row[key] else {}
                for reason, cnt in reasons.items():
                    block_csv_rows.append(
                        {
                            "preset_id": row["preset_id"],
                            "stage": stage.upper(),
                            "reason": reason,
                            "count": cnt,
                        }
                    )
    _write_csv(OUT / "l1_v3ap_block_statistics.csv", block_csv_rows)

    print(OUT / "l1_v3ap_parameter_matrix.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

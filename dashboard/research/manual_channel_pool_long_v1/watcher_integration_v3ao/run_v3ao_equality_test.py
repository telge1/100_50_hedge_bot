#!/usr/bin/env python3
"""V3AO equality tests — downstream must match V3AK for identical pending rows."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

PROBE = Path(__file__).resolve().parent.parent
V3AO = Path(__file__).resolve().parent
V3AK = PROBE / "substantial_entry_gate_v3ak"
V3AN = PROBE / "touch_watcher_research_v3an"
for p in (PROBE, V3AO, V3AK, V3AN):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import run_l1_pilot as l1
from v3ak_context import DEFAULT_SNAPSHOT, FROZEN_SHA256, build_context_from_snapshot
from watcher_integration_v3ao.pending_materialize import materialize_pending_row
from watcher_integration_v3ao.v3ao_pipeline import (
    compare_trade_lists,
    pending_candidates,
    prepare_pending_for_execution,
    run_pipeline_on_pending,
)

OUT = V3AO
REF_EIDS = (
    "te-d-c0791ea0c65e-0003",
    "te-d-ea081434c5d3-0003",
)


def _compare_rows(a: dict, b: dict, keys: tuple[str, ...]) -> list[str]:
    diffs: list[str] = []
    for k in keys:
        if str(a.get(k, "")) != str(b.get(k, "")):
            diffs.append(f"{k}: {a.get(k)!r} != {b.get(k)!r}")
    return diffs


def main() -> int:
    l1._bootstrap_import_paths()
    mod = sys.modules[l1.__name__]
    frozen = build_context_from_snapshot(DEFAULT_SNAPSHOT, expected_sha256=FROZEN_SHA256)
    lines = [ln for ln in frozen["ctx"]["lines_by_id"].values()]
    candles = frozen["candles"]
    lines_by_id = frozen["lines_by_id"]
    upper_line = frozen["upper_line"]

    report: dict = {"tests": [], "pass": True}

    # Test 1 — double pipeline on same deepcopy row → identical trades
    for eid in REF_EIDS:
        src = next((p for p in frozen["pending"] if p.get("touch_event_id") == eid and p.get("_entry_idx")), None)
        if src is None:
            report["tests"].append({"name": f"double_pipeline_{eid}", "skipped": "row_not_found"})
            continue
        row = copy.deepcopy(src)
        t1 = run_pipeline_on_pending(
            [row],
            lines=lines,
            candles=candles,
            upper_line=upper_line,
            lines_by_id=lines_by_id,
        )["executed"]
        t2 = run_pipeline_on_pending(
            [copy.deepcopy(src)],
            lines=lines,
            candles=candles,
            upper_line=upper_line,
            lines_by_id=lines_by_id,
        )["executed"]
        diff = compare_trade_lists(t1, t2)
        ok = not diff
        report["tests"].append({"name": f"double_pipeline_{eid}", "pass": ok, "mismatches": diff})
        report["pass"] = report["pass"] and ok

    # Test 2 — rematerialize + D2 enrich vs frozen row (pool SSOT at confirm bar)
    from pool_d2_metrics import apply_d2_gate_to_pending, enrich_d2_channel_filter_fields
    import pilot_engine as pe

    gate_keys = (
        "_pool_decision_close",
        "_entry_idx",
        "entry_price",
        "entry_utc",
        "confirm_utc",
        "_d2_gate_ok",
        "d2_gate_reason",
        "pool_ssot",
    )
    for eid in REF_EIDS:
        src = next((p for p in frozen["pending"] if p.get("touch_event_id") == eid and p.get("_entry_idx")), None)
        if src is None:
            continue
        line = lines_by_id[src["touch_drawing_id"]]
        rebuilt = materialize_pending_row(
            l1=mod,
            line=line,
            candles=candles,
            lines=lines,
            lines_by_id=lines_by_id,
            upper_line=upper_line,
            touch_idx=int(src["_touch_start_idx"]),
            confirm_idx=int(src["_entry_idx"]) - 1,
            entry_idx=int(src["_entry_idx"]),
            touch_event_id=src["touch_event_id"],
            line_selection_mode=src["line_selection_mode"],
            confirm_variant=src.get("confirm_variant", "C1"),
            struct_class=src["structure_class"],
            struct_reject=None,
        )
        apply_d2_gate_to_pending([rebuilt], lines, l1_module=mod, apply_provisional_research_filter=False)
        pe.enrich_pending_metrics([rebuilt], upper_line, mod, independent=True)
        enrich_d2_channel_filter_fields([rebuilt], upper_line, mod, independent=True)
        diffs = _compare_rows(src, rebuilt, gate_keys)
        report["tests"].append({"name": f"rematerialize_post_d2_{eid}", "pass": not diffs, "mismatches": diffs})
        report["pass"] = report["pass"] and not diffs

    # Test 3 — rematerialize row → same trade as original when run alone
    for eid in REF_EIDS:
        src = next((p for p in frozen["pending"] if p.get("touch_event_id") == eid and p.get("_entry_idx")), None)
        if src is None:
            continue
        line = lines_by_id[src["touch_drawing_id"]]
        rebuilt = materialize_pending_row(
            l1=mod,
            line=line,
            candles=candles,
            lines=lines,
            lines_by_id=lines_by_id,
            upper_line=upper_line,
            touch_idx=int(src["_touch_start_idx"]),
            confirm_idx=int(src["_entry_idx"]) - 1,
            entry_idx=int(src["_entry_idx"]),
            touch_event_id=src["touch_event_id"],
            line_selection_mode=src["line_selection_mode"],
            confirm_variant=src.get("confirm_variant", "C1"),
            struct_class=src["structure_class"],
            struct_reject=None,
        )
        apply_d2_gate_to_pending([rebuilt], lines, l1_module=mod, apply_provisional_research_filter=False)
        pe.enrich_pending_metrics([rebuilt], upper_line, mod, independent=True)
        enrich_d2_channel_filter_fields([rebuilt], upper_line, mod, independent=True)

        orig_trades = run_pipeline_on_pending([copy.deepcopy(src)], lines=lines, candles=candles, upper_line=upper_line, lines_by_id=lines_by_id)["executed"]
        new_trades = run_pipeline_on_pending([rebuilt], lines=lines, candles=candles, upper_line=upper_line, lines_by_id=lines_by_id)["executed"]
        diff = compare_trade_lists(orig_trades, new_trades)
        ok = not diff
        report["tests"].append({"name": f"rematerialize_exec_{eid}", "pass": ok, "mismatches": diff})
        report["pass"] = report["pass"] and ok

    # Test 4 — V3AK manifest baseline vs fresh E3 run (count only)
    manifest_path = V3AK / "l1_v3ak_run_manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = manifest.get("stats", {}).get("E3_SUBSTANTIAL_TP2c", {}).get("trades_executed")
        full = run_pipeline_on_pending(
            frozen["pending"],
            lines=lines,
            candles=candles,
            upper_line=upper_line,
            lines_by_id=lines_by_id,
        )["executed"]
        ok = expected is None or len(full) == int(expected)
        report["tests"].append(
            {
                "name": "v3ak_replay_trade_count",
                "pass": ok,
                "expected": expected,
                "actual": len(full),
            }
        )
        report["pass"] = report["pass"] and ok

    out = OUT / "l1_v3ao_equality_report.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(out)
    if not report["pass"]:
        raise SystemExit(1)
    print("V3AO equality: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""L1 V3AK — Substantial upper-pool entry gate on V3AH CAP6-D20-BLOCK baseline."""

from __future__ import annotations

import copy
import csv
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

V3AK = Path(__file__).resolve().parent
PROBE = V3AK.parent
V3AB = PROBE / "support_break_exit_v3ab"
V3Y = PROBE / "support_break_exit_v3y"
V3AH = PROBE / "realistic_pool_tp_v3ah"
V3W = PROBE / "tp_research_v3w"
DASHBOARD = PROBE.parent

for p in (V3AK, V3AH, V3W, V3AB, PROBE, V3Y, DASHBOARD):
    if p.is_dir() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pilot_engine as pe
import run_l1_pilot as l1
from v3y_config import CONFIRM_DELTA_PCT
from zone_density import RealisticTPParams

import importlib.util

_v3y_engine = importlib.util.spec_from_file_location("v3y_engine", V3Y / "engine.py")
assert _v3y_engine and _v3y_engine.loader
_v3y_engine_mod = importlib.util.module_from_spec(_v3y_engine)
_v3y_engine.loader.exec_module(_v3y_engine_mod)
execute_variant_v3y = _v3y_engine_mod.execute_variant_v3y

from substantial_entry_gate import ENTRY_GATE_SPECS, apply_substantial_entry_gate, evaluate_substantial_near_zone
from v3ah_tp import pending_with_realistic_tp
from v3ak_context import (
    DEFAULT_SNAPSHOT,
    FROZEN_SHA256,
    TP_SCENARIO,
    VARIANT_B,
    build_context_from_snapshot,
)

OUT = V3AK
RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
REF_EID = "te-d-c0791ea0c65e-0003"
CAP6_TP = RealisticTPParams(
    code=TP_SCENARIO,
    cap_dist_pct=6.0,
    dominance="D20",
    no_tp_policy="BLOCK",
)


def _executed(trades: list[dict]) -> list[dict]:
    return [t for t in trades if t.get("exit_kind") not in ("", "NO_TRADE", None)]


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


def _pending_candidates(pending: list[dict]) -> list[dict]:
    return [
        p
        for p in pending
        if p.get("long_trigger_allowed") == "yes" and p.get("_entry_idx")
    ]


def _would_pass_v3e_b(row: dict, pending_base: dict) -> tuple[bool, str]:
    """Variant B filters without substantial gate (uses row after CAP6 TP patch)."""
    if not row.get("_d2_gate_ok"):
        return False, row.get("d2_gate_reason") or "D2_FAIL"
    if not row.get("_d2_nearness_ok"):
        return False, row.get("d2_nearness_reason") or "NEARNESS_FAIL"
    if not row.get("_pick_ok"):
        return False, row.get("reject_reason") or "PICK_FAIL"
    return True, ""


def main() -> int:
    l1._bootstrap_import_paths()
    mod = sys.modules[l1.__name__]
    frozen = build_context_from_snapshot(DEFAULT_SNAPSHOT, expected_sha256=FROZEN_SHA256)
    lines = [ln for ln in frozen["ctx"]["lines_by_id"].values()]
    candles = frozen["candles"]
    lines_by_id = frozen["lines_by_id"]
    upper_line = frozen["upper_line"]

    tp_pending = pending_with_realistic_tp(
        copy.deepcopy(frozen["pending"]), lines, l1_module=mod, params=CAP6_TP
    )

    entry_decisions: list[dict] = []
    trade_rows: list[dict] = []
    chart_rows: list[dict] = []
    stats: dict[str, Any] = {}
    executed_ids_by_spec: dict[str, set[str]] = {}

    for spec in ENTRY_GATE_SPECS:
        pending = copy.deepcopy(tp_pending)
        apply_substantial_entry_gate(pending, spec)
        pe.enrich_pending_metrics(pending, upper_line, mod, independent=True)

        for row in _pending_candidates(pending):
            vok, vreason = _would_pass_v3e_b(row, row)
            meta = {}
            if row.get("_v3ak_entry_meta"):
                try:
                    meta = json.loads(row["_v3ak_entry_meta"])
                except json.JSONDecodeError:
                    pass
            qz = meta.get("qualifying_zone") or {}
            entry_decisions.append(
                {
                    "entry_gate_model": spec.code,
                    "touch_event_id": row["touch_event_id"],
                    "entry_utc": row.get("entry_utc", ""),
                    "entry_price": row.get("entry_price", ""),
                    "substantial_gate_pass": row.get("entry_gate_pass", "n/a" if spec.params is None else ""),
                    "entry_gate_reason": row.get("entry_gate_reason", ""),
                    "fail_detail": meta.get("fail_detail", ""),
                    "v3e_b_would_pass_excl_substantial": "yes" if vok else "no",
                    "v3e_b_block_reason": vreason,
                    "qual_zone_bottom": qz.get("bottom", ""),
                    "qual_zone_top": qz.get("top", ""),
                    "qual_zone_width_pct": qz.get("width_pct", ""),
                    "qual_zone_max_depth": qz.get("max_depth", ""),
                    "qual_zone_origins": qz.get("unique_origins", ""),
                    "qual_zone_dist_pct": qz.get("dist_from_entry_pct", ""),
                    "cap6_tp_price": row.get("tp_price", ""),
                    "cap6_pick_ok": row.get("_pick_ok", ""),
                    "retrospective_drawing": "RETROSPECTIVE_DRAWING",
                }
            )

        trades = execute_variant_v3y(
            VARIANT_B,
            copy.deepcopy(pending),
            candles,
            upper_line=upper_line,
            lines_by_id=lines_by_id,
            l1_module=mod,
        )
        ex = _executed(trades)
        executed_ids_by_spec[spec.code] = {t["touch_event_id"] for t in ex}
        ec = Counter(str(t.get("exit_kind") or "") for t in ex)
        w = sum(1 for t in ex if float(t.get("gross_pnl_pct") or 0) > 0)
        lo = sum(1 for t in ex if float(t.get("gross_pnl_pct") or 0) < 0)
        gross = round(sum(float(t["gross_pnl_pct"]) for t in ex if t.get("gross_pnl_pct")), 4)
        loss_streak = 0
        best_ls = 0
        for t in ex:
            if float(t.get("gross_pnl_pct") or 0) < 0:
                loss_streak += 1
                best_ls = max(best_ls, loss_streak)
            else:
                loss_streak = 0

        blocked_substantial = sum(
            1 for p in _pending_candidates(pending) if p.get("entry_gate_pass") == "no"
        )
        stats[spec.code] = {
            "entry_candidates": len(_pending_candidates(pending)),
            "trades_executed": len(ex),
            "blocked_substantial_gate": blocked_substantial if spec.params else 0,
            "wins": w,
            "losses": lo,
            "exit_counts": dict(ec),
            "gross_pct_sum": gross,
            "avg_pct_per_trade": round(gross / len(ex), 4) if ex else 0.0,
            "longest_loss_streak": best_ls,
        }

        pmap = {p["touch_event_id"]: p for p in pending}
        trade_no = 0
        for t in ex:
            trade_no += 1
            prow = pmap.get(t.get("touch_event_id", ""), {})
            meta = json.loads(prow.get("_v3ak_entry_meta") or "{}") if prow.get("_v3ak_entry_meta") else {}
            qz = meta.get("qualifying_zone") or {}
            trade_rows.append(
                {
                    "entry_gate_model": spec.code,
                    "trade_no": trade_no,
                    "entry_utc": t.get("entry_utc", ""),
                    "entry_price": t.get("entry_price", ""),
                    "support_id": t.get("touch_drawing_id", t.get("support_drawing_id", "")),
                    "entry_gate_zone_lo": qz.get("bottom", ""),
                    "entry_gate_zone_hi": qz.get("top", ""),
                    "tp_price": t.get("tp_price", ""),
                    "sl_price": t.get("sl_price", ""),
                    "exit_utc": t.get("exit_utc", ""),
                    "exit_price": t.get("exit_price", ""),
                    "exit_reason": t.get("exit_kind", ""),
                    "pnl_pct": t.get("gross_pnl_pct", ""),
                    "retrospective": "RETROSPECTIVE_DRAWING",
                }
            )
            chart_rows.append(
                {
                    "entry_gate_model": spec.code,
                    "trade_no": trade_no,
                    "entry_utc": t.get("entry_utc", ""),
                    "entry_price": t.get("entry_price", ""),
                    "support_id": t.get("touch_drawing_id", ""),
                    "confirm_utc": prow.get("confirm_utc", ""),
                    "entry_gate_zone_lo": qz.get("bottom", ""),
                    "entry_gate_zone_hi": qz.get("top", ""),
                    "entry_gate_width_pct": qz.get("width_pct", ""),
                    "entry_gate_max_depth": qz.get("max_depth", ""),
                    "entry_gate_origins": qz.get("unique_origins", ""),
                    "entry_gate_dist_pct": qz.get("dist_from_entry_pct", ""),
                    "cap6_tp_price": t.get("tp_price", ""),
                    "sl_price": t.get("sl_price", ""),
                    "exit_utc": t.get("exit_utc", ""),
                    "exit_price": t.get("exit_price", ""),
                    "exit_reason": t.get("exit_kind", ""),
                    "pnl_pct": t.get("gross_pnl_pct", ""),
                    "retrospective_drawing": "RETROSPECTIVE_DRAWING",
                }
            )

    e0_ids = executed_ids_by_spec.get("E0_BASELINE", set())
    for spec in ENTRY_GATE_SPECS:
        ids = executed_ids_by_spec.get(spec.code, set())
        stats[spec.code]["new_slots_vs_E0"] = sorted(ids - e0_ids)
        stats[spec.code]["dropped_vs_E0"] = sorted(e0_ids - ids)

    # Reference trade
    ref_row = next(p for p in tp_pending if p["touch_event_id"] == REF_EID)
    ref_reports = []
    from datetime import datetime as dt

    dc = dt.strptime(ref_row["_pool_decision_close"], "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
    entry = float(ref_row["entry_price"])
    for spec in ENTRY_GATE_SPECS:
        if spec.params is None:
            ref_reports.append({"model": spec.code, "pass": True, "zone": "n/a"})
            continue
        res = evaluate_substantial_near_zone(ref_row["_pool_members"], dc, entry, spec.params)
        z = res.qualifying_zone
        ref_reports.append(
            {
                "model": spec.code,
                "pass": res.pass_gate,
                "zone": f"{z.bottom}-{z.top}" if z else "",
                "detail": res.fail_detail,
            }
        )

    manifest = {
        "run_id": RUN_ID,
        "drawings_sha256": frozen["drawings_sha256"],
        "snapshot": str(DEFAULT_SNAPSHOT),
        "variant": VARIANT_B.code,
        "tp_model": TP_SCENARIO,
        "confirm_delta_pct": CONFIRM_DELTA_PCT,
        "sl_pct": 1.20,
        "near_cap_pct": 2.50,
        "entry_gate_specs": [
            {
                "code": s.code,
                "params": (
                    None
                    if s.params is None
                    else {
                        "min_width_pct": s.params.min_width_pct,
                        "min_depth": s.params.min_depth,
                        "min_origins": s.params.min_origins,
                    }
                ),
            }
            for s in ENTRY_GATE_SPECS
        ],
        "stats": stats,
        "reference_trade": ref_reports,
    }

    _write_csv(OUT / "l1_v3ak_entry_gate_decisions.csv", entry_decisions)
    _write_csv(OUT / "l1_v3ak_trades_all.csv", trade_rows)
    _write_csv(OUT / "l1_v3ak_chart_review.csv", chart_rows)
    _write_csv(OUT / "l1_v3ak_summary.csv", [{"model": k, **v} for k, v in stats.items()])
    (OUT / "l1_v3ak_run_manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")

    report = _build_report(manifest, stats, ref_reports)
    (OUT / "L1_V3AK_SUBSTANTIAL_ENTRY_GATE_REPORT.md").write_text(report, encoding="utf-8")
    print(OUT / "L1_V3AK_SUBSTANTIAL_ENTRY_GATE_REPORT.md")
    return 0


def _build_run_pending(tp_pending: list[dict], spec) -> list[dict]:
    p = copy.deepcopy(tp_pending)
    apply_substantial_entry_gate(p, spec)
    return p


def _build_report(manifest: dict, stats: dict, ref: list[dict]) -> str:
    lines = [
        "# L1 V3AK — Substantial Upper-Pool Entry Gate",
        "",
        f"Run: `{manifest['run_id']}` · Snapshot `{manifest['drawings_sha256'][:16]}…`",
        f"Variante **V3E-B** · TP fix **{manifest['tp_model']}** (unverändert vs. V3AH)",
        "",
        "## Summary",
        "",
        "| Model | Kandidaten | Trades | Block Gate | TP | SBE | SL | Σ% | Ø/Trade |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for code, s in stats.items():
        ec = s.get("exit_counts", {})
        lines.append(
            f"| {code} | {s['entry_candidates']} | {s['trades_executed']} | {s['blocked_substantial_gate']} | "
            f"{ec.get('TP', 0)} | {ec.get('SUPPORT_BREAK_EXIT', 0)} | {ec.get('SL', 0)} | "
            f"{s['gross_pct_sum']:.4f} | {s['avg_pct_per_trade']:.4f} |"
        )
    lines.extend(["", "## Referenztrade", ""])
    for r in ref:
        lines.append(f"- **{r['model']}**: pass={r['pass']} zone=`{r.get('zone','')}` {r.get('detail','')}")
    lines.extend(
        [
            "",
            "## Artefakte",
            "",
            "- `l1_v3ak_entry_gate_decisions.csv`",
            "- `l1_v3ak_trades_all.csv`",
            "- `l1_v3ak_chart_review.csv`",
            "- `l1_v3ak_summary.csv`",
            "",
            "**STOP** — keine Übernahme in V3AB/V3AH/V3Y.",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())

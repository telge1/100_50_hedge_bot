#!/usr/bin/env python3
"""L1 V3J — DRAWINGS_AUTO_WINDOW long backtest (no manual start/end)."""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROBE_DIR = Path(__file__).resolve().parent
DRAWINGS_PATH = PROBE_DIR.parents[1] / "research_charts" / "user_data" / "drawings.json"

if str(PROBE_DIR) not in sys.path:
    sys.path.insert(0, str(PROBE_DIR))
_TOUCH = PROBE_DIR.parent / "manual_line_touch_probe_v1"
if str(_TOUCH) not in sys.path:
    sys.path.insert(0, str(_TOUCH))

import pilot_engine as pe
import run_l1_pilot as l1
import run_l1_v3g_nearness_staggering as v3g
from config import L1_V3E_VARIANT_A, L1_V3E_VARIANT_B, L1_V3E_VARIANT_C
from drawings_auto_window import (
    DRAWINGS_AUTO_WINDOW,
    STATUS_DATA_END,
    STATUS_LINES_EXHAUSTED,
    STATUS_NO_ELIGIBLE,
    compute_auto_window,
    fetch_last_available_bar_open,
    is_candidate_support_trend,
)
from line_geometry import BAR_SEC_5M, LineDef, fmt_utc
from pool_d2_metrics import apply_d2_gate_to_pending, enrich_d2_channel_filter_fields
from support_line_selection import LINE_SELECTION_DYNAMIC, line_anchor_record

OUT_DIR = PROBE_DIR
VARIANTS = {"A": L1_V3E_VARIANT_A, "B": L1_V3E_VARIANT_B, "C": L1_V3E_VARIANT_C}


def _resolve_run_end_status(
    auto_status: str,
    support_lines: list[LineDef],
    data_end_bar_open: int,
) -> str:
    if auto_status == STATUS_NO_ELIGIBLE:
        return STATUS_NO_ELIGIBLE
    if any(ln.extend_right for ln in support_lines):
        return STATUS_DATA_END
    ends = [int(ln.t_hi) for ln in support_lines if ln.t_hi is not None]
    if ends and max(ends) < data_end_bar_open:
        return STATUS_LINES_EXHAUSTED
    return STATUS_DATA_END


def _line_stats(pending: list[dict], trades_a: list[dict]) -> list[dict]:
    by_id: dict[str, dict] = {}
    exe = {t["touch_event_id"] for t in v3g._executed(trades_a)}
    for row in pending:
        did = row.get("touch_drawing_id") or ""
        if did not in by_id:
            by_id[did] = {
                "drawing_id": did,
                "touches": 0,
                "long_allowed": 0,
                "c1_entries": 0,
                "executed_A": 0,
                "causal_touches": 0,
                "retro_touches": 0,
            }
        st = by_id[did]
        st["touches"] += 1
        if row.get("long_trigger_allowed") == "yes":
            st["long_allowed"] += 1
        if row.get("entry_utc"):
            st["c1_entries"] += 1
        if row.get("drawing_causality") == "CAUSAL":
            st["causal_touches"] += 1
        else:
            st["retro_touches"] += 1
        if row.get("touch_event_id") in exe:
            st["executed_A"] += 1
    return sorted(by_id.values(), key=lambda x: x["drawing_id"])


def _trade_signature(trades: list[dict]) -> list[tuple]:
    out = []
    for t in v3g._executed(trades):
        out.append(
            (
                t.get("entry_utc", ""),
                t.get("touch_drawing_id", ""),
                t.get("exit_kind", ""),
                round(float(t.get("gross_pnl_pct") or 0), 4),
            )
        )
    return sorted(out)


def _run_v3j_context(
    *,
    lines: list[LineDef],
    ws: int,
    we: int,
    sim_end: int,
) -> dict[str, Any]:
    saved_limit = l1.CANDLE_LOAD_LIMIT
    l1.CANDLE_LOAD_LIMIT = 10000
    try:
        ctx = l1.build_pilot_context(
            line_selection_mode=LINE_SELECTION_DYNAMIC,
            window_start=ws,
            window_end=we,
            simulation_end=sim_end,
        )
    finally:
        l1.CANDLE_LOAD_LIMIT = saved_limit
    if ctx is None:
        raise RuntimeError("build_pilot_context failed")
    mod = sys.modules[l1.__name__]
    pending = copy.deepcopy(ctx["pending"])
    apply_d2_gate_to_pending(pending, lines, l1_module=mod, apply_provisional_research_filter=False)
    pe.enrich_pending_metrics(pending, ctx["upper_line"], mod)
    enrich_d2_channel_filter_fields(pending, ctx["upper_line"], mod)

    trades: dict[str, list[dict]] = {}
    summary: dict[str, dict] = {}
    for code, var in VARIANTS.items():
        trades[code] = pe.execute_variant(
            var, copy.deepcopy(pending), ctx["candles"], upper_line=ctx["upper_line"], l1_module=mod
        )
        sm = v3g._summary_metrics(trades[code], ctx["touch_stats"])
        sm["admitted"] = v3g._admitted_candidates(
            var, pending, ctx["candles"], upper_line=ctx["upper_line"], l1_module=mod
        )
        summary[code] = sm
    return {"ctx": ctx, "pending": pending, "trades": trades, "summary": summary}


def main() -> int:
    l1._bootstrap_import_paths()
    if not DRAWINGS_PATH.is_file():
        print(f"MISSING_DRAWINGS {DRAWINGS_PATH}")
        return 2

    text = DRAWINGS_PATH.read_text(encoding="utf-8")
    sha = hashlib.sha256(text.encode()).hexdigest()
    payload = json.loads(text)
    n_drawings = len(payload.get("drawings") or [])
    snap_path = OUT_DIR / f"l1_v3j_drawings_snapshot_{sha[:16]}.json"
    if not snap_path.is_file():
        snap_path.write_text(text, encoding="utf-8")

    lines = [ln for ln in (l1.line_from_drawing(d) for d in payload.get("drawings") or []) if ln]
    data_end = fetch_last_available_bar_open(
        l1.SYMBOL, l1.TIMEFRAME, bootstrap=l1._bootstrap_import_paths
    )
    sim_end = data_end + BAR_SEC_5M
    auto = compute_auto_window(
        lines,
        upper_main_id=l1.UPPER_MAIN_CHANNEL_LINE_ID,
        data_end_bar_open=data_end,
    )
    support_lines = [
        ln
        for ln in lines
        if is_candidate_support_trend(ln, upper_main_id=l1.UPPER_MAIN_CHANNEL_LINE_ID)
    ]
    run_end = _resolve_run_end_status(auto.status, support_lines, data_end)

    manifest: dict[str, Any] = {
        "mode": DRAWINGS_AUTO_WINDOW,
        "line_selection_mode": LINE_SELECTION_DYNAMIC,
        "drawings_sha256": sha,
        "drawings_count": n_drawings,
        "support_line_count": len(support_lines),
        "support_line_ids": list(auto.support_line_ids),
        "window_start_utc": fmt_utc(auto.window_start_ts),
        "window_end_utc_exclusive": fmt_utc(auto.window_end_ts),
        "data_end_bar_utc": fmt_utc(data_end),
        "simulation_end_utc_exclusive": fmt_utc(sim_end),
        "run_end_status": run_end,
        "per_line_research_window": list(auto.per_line),
        "snapshot_path": str(snap_path),
    }

    if run_end == STATUS_NO_ELIGIBLE:
        (OUT_DIR / "l1_v3j_run_manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        (OUT_DIR / "l1_v3j_auto_window_report.md").write_text(
            "\n".join(
                [
                    "# L1 V3J — DRAWINGS_AUTO_WINDOW",
                    "",
                    f"- **Status**: `{run_end}`",
                    f"- Drawings SHA256: `{sha}`",
                    f"- Drawings count: {n_drawings}",
                    "- No eligible diagonal support trend lines.",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        for name in (
            "l1_v3j_support_lines.csv",
            "l1_v3j_candidates.csv",
            "l1_v3j_variant_a_trades.csv",
            "l1_v3j_variant_b_trades.csv",
            "l1_v3j_variant_c_trades.csv",
        ):
            (OUT_DIR / name).write_text("", encoding="utf-8")
        return 0

    result = _run_v3j_context(
        lines=lines,
        ws=auto.window_start_ts,
        we=auto.window_end_ts,
        sim_end=sim_end,
    )
    ctx = result["ctx"]
    pending = result["pending"]
    trades = result["trades"]
    summary = result["summary"]

    # V3I uses the same build_pilot_context + DYNAMIC_SUPPORT path with explicit window bounds.
    parity: dict[str, Any] = {}
    for code in "ABC":
        sig = _trade_signature(result["trades"][code])
        parity[code] = {"v3j_engine": "DYNAMIC_SUPPORT", "trade_signature": sig, "n": len(sig)}
    manifest["v3i_dynamic_parity"] = parity
    manifest["candle_count"] = len(ctx["candles"])
    manifest["touch_events_total"] = ctx["touch_stats"]["touch_events_total"]
    manifest["long_triggers_allowed"] = ctx["touch_stats"]["long_triggers_allowed"]
    manifest["c1_candidates"] = sum(1 for r in pending if r.get("entry_utc"))
    for code in "ABC":
        manifest[f"variant_{code.lower()}_trades"] = len(v3g._executed(trades[code]))
        manifest[f"variant_{code.lower()}_gross_pct"] = summary[code]["gross_realized_sum"]

    retro = sum(1 for t in v3g._executed(trades["A"]) if t.get("drawing_causality") == "RETROSPECTIVE_DRAWING")

    support_rows = []
    for ln in support_lines:
        rec = line_anchor_record(ln)
        rec.update(next((s for s in _line_stats(pending, trades["A"]) if s["drawing_id"] == ln.drawing_id), {}))
        pl = next((p for p in auto.per_line if p["drawing_id"] == ln.drawing_id), {})
        rec["research_start_utc"] = pl.get("research_start_utc", "")
        rec["research_end_utc"] = pl.get("research_end_utc", "")
        support_rows.append(rec)

    with (OUT_DIR / "l1_v3j_support_lines.csv").open("w", newline="", encoding="utf-8") as f:
        if support_rows:
            w = csv.DictWriter(f, fieldnames=sorted({k for r in support_rows for k in r}))
            w.writeheader()
            w.writerows(support_rows)

    cand_rows = []
    for row in pending:
        if not row.get("entry_utc"):
            continue
        base = {k: v for k, v in row.items() if not str(k).startswith("_")}
        dec = v3g._filter_decisions(row, VARIANTS)
        base["filter_A"], base["filter_B"], base["filter_C"] = dec["A"], dec["B"], dec["C"]
        cand_rows.append(base)
    with (OUT_DIR / "l1_v3j_candidates.csv").open("w", newline="", encoding="utf-8") as f:
        if cand_rows:
            w = csv.DictWriter(f, fieldnames=sorted({k for r in cand_rows for k in r}))
            w.writeheader()
            w.writerows(cand_rows)

    for code in ("A", "B", "C"):
        v3g._write_trades_csv(OUT_DIR / f"l1_v3j_variant_{code.lower()}_trades.csv", trades[code])

    wins = sum(1 for t in v3g._executed(trades["A"]) if float(t.get("gross_pnl_pct") or 0) > 0)
    sls = sum(1 for t in v3g._executed(trades["A"]) if t.get("exit_kind") == "SL")

    report = [
        "# L1 V3J — DRAWINGS_AUTO_WINDOW",
        "",
        f"- **Drawings SHA256**: `{sha}`",
        f"- **Drawings count**: {n_drawings}",
        f"- **Eligible support trend lines**: {len(support_lines)}",
        f"- **Earliest support research start**: {fmt_utc(auto.window_start_ts)}",
        f"- **Latest support research end (geometry)**: {fmt_utc(auto.window_end_ts - BAR_SEC_5M)}",
        f"- **Candle window**: {fmt_utc(auto.window_start_ts)} .. {fmt_utc(sim_end)} (simulation exclusive)",
        f"- **5m bars loaded**: {len(ctx['candles'])}",
        f"- **Run end status**: `{run_end}`",
        f"- **Touch events (all lines)**: {ctx['touch_stats']['touch_events_total']}",
        f"- **Long triggers allowed**: {ctx['touch_stats']['long_triggers_allowed']}",
        f"- **C1 candidates**: {manifest['c1_candidates']}",
        "",
        "## Variants (gross % sum, uncompounded)",
        "",
    ]
    for code in "ABC":
        sm = summary[code]
        report.append(
            f"- **{code}**: trades={len(v3g._executed(trades[code]))}, "
            f"gross={sm['gross_realized_sum']:.4f}%, admitted={sm['admitted']}"
        )
    report.extend(
        [
            "",
            f"- **Wins (A)**: {wins}",
            f"- **SL exits (A)**: {sls}",
            f"- **Retrospective executed (A)**: {retro}",
            "",
            "## V3I DYNAMIC_SUPPORT parity (same window)",
            "",
        ]
    )
    for code in "ABC":
        p = parity[code]
        report.append(f"- **{code}**: trades={p['n']} (same engine as V3I DYNAMIC_SUPPORT with this window)")

    (OUT_DIR / "l1_v3j_auto_window_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    (OUT_DIR / "l1_v3j_run_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""L1 V3L — INDEPENDENT_SUPPORT vs V3J DYNAMIC (DRAWINGS_AUTO_WINDOW)."""

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
SEP_LINE = "d-1f70c9798935"
SEP_START = utc_ts = None  # filled after import

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
    compute_auto_window,
    fetch_last_available_bar_open,
)
from line_geometry import BAR_SEC_5M, fmt_utc, utc_ts
from pool_d2_metrics import apply_d2_gate_to_pending, enrich_d2_channel_filter_fields
from support_line_selection import LINE_SELECTION_DYNAMIC, LINE_SELECTION_INDEPENDENT

SEP_START = utc_ts(datetime(2026, 9, 16, 0, 0, 0, tzinfo=timezone.utc))
SEP_END = utc_ts(datetime(2026, 10, 2, 4, 0, 0, tzinfo=timezone.utc))

OUT_DIR = PROBE_DIR
VARIANTS = {"A": L1_V3E_VARIANT_A, "B": L1_V3E_VARIANT_B, "C": L1_V3E_VARIANT_C}


def _run(mode: str) -> dict[str, Any]:
    l1._bootstrap_import_paths()
    payload = json.loads(DRAWINGS_PATH.read_text(encoding="utf-8"))
    lines = [ln for ln in (l1.line_from_drawing(d) for d in payload.get("drawings") or []) if ln]
    data_end = fetch_last_available_bar_open(l1.SYMBOL, l1.TIMEFRAME, bootstrap=l1._bootstrap_import_paths)
    auto = compute_auto_window(
        lines, upper_main_id=l1.UPPER_MAIN_CHANNEL_LINE_ID, data_end_bar_open=data_end
    )
    saved = l1.CANDLE_LOAD_LIMIT
    l1.CANDLE_LOAD_LIMIT = 100000
    try:
        ctx = l1.build_pilot_context(
            line_selection_mode=mode,
            window_start=auto.window_start_ts,
            window_end=auto.window_end_ts,
            simulation_end=data_end + BAR_SEC_5M,
        )
    finally:
        l1.CANDLE_LOAD_LIMIT = saved
    if ctx is None:
        raise RuntimeError("build_pilot_context failed")
    mod = sys.modules[l1.__name__]
    indie = mode == LINE_SELECTION_INDEPENDENT
    pending = copy.deepcopy(ctx["pending"])
    apply_d2_gate_to_pending(pending, lines, l1_module=mod, apply_provisional_research_filter=False)
    pe.enrich_pending_metrics(pending, ctx["upper_line"], mod, independent=indie)
    enrich_d2_channel_filter_fields(pending, ctx["upper_line"], mod, independent=indie)

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
    return {
        "mode": mode,
        "ctx": ctx,
        "pending": pending,
        "trades": trades,
        "summary": summary,
        "auto": auto,
        "data_end": data_end,
    }


def _executed_sig(trades: list[dict]) -> list[tuple]:
    out = []
    for t in v3g._executed(trades):
        out.append(
            (
                t.get("entry_utc", ""),
                t.get("touch_drawing_id", ""),
                t.get("exit_kind", ""),
                round(float(t.get("gross_pnl_pct") or 0), 4),
                t.get("tp_mode", ""),
            )
        )
    return sorted(out)


def _sep_rows(pending: list[dict], trades_a: list[dict]) -> list[dict]:
    exe = {t["touch_event_id"] for t in v3g._executed(trades_a)}
    rows = []
    for row in pending:
        if row.get("touch_drawing_id") != SEP_LINE:
            continue
        if not row.get("entry_utc"):
            continue
        dec = v3g._filter_decisions(row, VARIANTS)
        rows.append(
            {
                "touch_utc": row.get("touch_utc", ""),
                "c1_utc": row.get("confirm_utc", ""),
                "entry_utc": row.get("entry_utc", ""),
                "entry_price": row.get("entry_price", ""),
                "support_id": SEP_LINE,
                "d2_nearest_pct": row.get("d2_nearest_dist_pct") or row.get("d2_first_in_channel_pct", ""),
                "tp_mode": row.get("tp_mode", ""),
                "tp_price": row.get("tp_price", ""),
                "d2_nearness_pass": row.get("d2_nearness_pass", ""),
                "filter_A": dec["A"],
                "filter_B": dec["B"],
                "filter_C": dec["C"],
                "reject_reason": row.get("reject_reason", ""),
                "executed_A": row.get("touch_event_id") in exe,
                "causality": row.get("drawing_causality", ""),
            }
        )
    return sorted(rows, key=lambda r: r["entry_utc"])


def main() -> int:
    text = DRAWINGS_PATH.read_text(encoding="utf-8")
    sha = hashlib.sha256(text.encode()).hexdigest()

    v3j = _run(LINE_SELECTION_DYNAMIC)
    v3l = _run(LINE_SELECTION_INDEPENDENT)

    # candidates csv (V3L)
    cand = []
    for row in v3l["pending"]:
        if not row.get("entry_utc"):
            continue
        base = {k: v for k, v in row.items() if not str(k).startswith("_")}
        dec = v3g._filter_decisions(row, VARIANTS)
        base["filter_A"], base["filter_B"], base["filter_C"] = dec["A"], dec["B"], dec["C"]
        cand.append(base)
    with (OUT_DIR / "l1_v3l_candidates.csv").open("w", newline="", encoding="utf-8") as f:
        if cand:
            w = csv.DictWriter(f, fieldnames=sorted({k for r in cand for k in r}))
            w.writeheader()
            w.writerows(cand)

    for code in ("A", "B", "C"):
        v3g._write_trades_csv(OUT_DIR / f"l1_v3l_variant_{code.lower()}_trades.csv", v3l["trades"][code])

    comp_rows = []
    for code in "ABC":
        sj = _executed_sig(v3j["trades"][code])
        sl = _executed_sig(v3l["trades"][code])
        comp_rows.append(
            {
                "variant": code,
                "v3j_trades": len(sj),
                "v3l_trades": len(sl),
                "match": sj == sl,
                "v3j_only": [x for x in sj if x not in sl],
                "v3l_only": [x for x in sl if x not in sj],
            }
        )
    with (OUT_DIR / "l1_v3l_comparison.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["variant", "v3j_trades", "v3l_trades", "match", "v3j_only", "v3l_only"],
        )
        w.writeheader()
        for r in comp_rows:
            r = dict(r)
            r["v3j_only"] = json.dumps(r["v3j_only"])
            r["v3l_only"] = json.dumps(r["v3l_only"])
            w.writerow(r)

    sep_pending = [r for r in v3l["pending"] if r.get("touch_drawing_id") == SEP_LINE]
    sep_c1_touch = sum(1 for r in sep_pending if r.get("entry_utc"))
    sep_long_ok = sum(1 for r in sep_pending if r.get("long_trigger_allowed") == "yes")
    sep_detail = _sep_rows(v3l["pending"], v3l["trades"]["A"])

    new_sep_trades = [
        t
        for t in v3g._executed(v3l["trades"]["A"])
        if t.get("touch_drawing_id") == SEP_LINE
        and _parse_entry_ts(t.get("entry_utc", "")) >= SEP_START
    ]

    manifest = {
        "mode": "INDEPENDENT_SUPPORT",
        "window_mode": DRAWINGS_AUTO_WINDOW,
        "drawings_sha256": sha,
        "v3j_line_mode": LINE_SELECTION_DYNAMIC,
        "v3l_line_mode": LINE_SELECTION_INDEPENDENT,
        "comparison": {r["variant"]: {k: r[k] for k in r if k not in ("v3j_only", "v3l_only")} for r in comp_rows},
        "sep_line": SEP_LINE,
        "sep_c1_pipeline_count": sep_c1_touch,
        "sep_long_triggers": sep_long_ok,
        "sep_new_trades_A": len(new_sep_trades),
        "variant_gross": {c: v3l["summary"][c]["gross_realized_sum"] for c in "ABC"},
        "variant_trades": {c: len(v3g._executed(v3l["trades"][c])) for c in "ABC"},
    }
    (OUT_DIR / "l1_v3l_run_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    md = _build_report(sha, v3j, v3l, comp_rows, sep_detail, manifest)
    (OUT_DIR / "l1_v3l_independent_support_report.md").write_text(md, encoding="utf-8")

    if not all(r["match"] for r in comp_rows if r["variant"] == "A" and False):
        pass
  # V3L expected to differ from V3J; only require V3J self-consistency would be separate
    return 0


def _parse_entry_ts(s: str) -> int:
    if not s:
        return 0
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc).timestamp())


def _build_report(
    sha: str,
    v3j: dict,
    v3l: dict,
    comp_rows: list[dict],
    sep_detail: list[dict],
    manifest: dict,
) -> str:
    lines = [
        "# L1 V3L — Independent Support vs V3J Original",
        "",
        f"- Drawings SHA256: `{sha}`",
        f"- Window: `{fmt_utc(v3l['auto'].window_start_ts)}` .. `{fmt_utc(v3l['data_end'] + BAR_SEC_5M)}`",
        "",
        "## Comparison",
        "",
    ]
    for r in comp_rows:
        lines.append(
            f"- **{r['variant']}**: V3J={r['v3j_trades']} trades, V3L={r['v3l_trades']} trades, identical={r['match']}"
        )
    lines.extend(["", "## V3L Variant stats", ""])
    for code in "ABC":
        sm = v3l["summary"][code]
        lines.append(
            f"- **{code}**: executed={len(v3g._executed(v3l['trades'][code]))}, "
            f"gross={sm['gross_realized_sum']:.4f}%"
        )
    lines.extend(
        [
            "",
            f"## September line `{SEP_LINE}`",
            "",
            f"- Long triggers allowed (V3L): {manifest['sep_long_triggers']}",
            f"- C1 pipeline rows: {manifest['sep_c1_pipeline_count']}",
            f"- Executed trades (A, Sep window): {manifest['sep_new_trades_A']}",
            "",
            "### C1 candidates (V3L)",
            "",
            "| Touch UTC | C1 UTC | Entry UTC | Entry | TP mode | TP | A | B | C | Reject |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for r in sep_detail:
        lines.append(
            f"| {r['touch_utc']} | {r['c1_utc']} | {r['entry_utc']} | {r['entry_price']} | "
            f"{r['tp_mode']} | {r['tp_price']} | {r['filter_A']} | {r['filter_B']} | {r['filter_C']} | {r['reject_reason']} |"
        )
    lines.extend(["", "## Executed trades V3L", ""])
    for code in "ABC":
        lines.append(f"### Variant {code}")
        lines.append("")
        lines.append(
            "| Entry UTC | Entry | SL | TP | Exit UTC | Exit | Kind | PnL % | drawing_id | TP mode |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for t in sorted(v3g._executed(v3l["trades"][code]), key=lambda x: x.get("entry_utc", "")):
            lines.append(
                f"| {t.get('entry_utc','')} | {t.get('entry_price','')} | {t.get('sl_price','')} | "
                f"{t.get('tp_price','')} | {t.get('exit_utc','')} | {t.get('exit_price','')} | "
                f"{t.get('exit_kind','')} | {t.get('gross_pnl_pct','')} | {t.get('touch_drawing_id','')} | "
                f"{t.get('tp_mode','')} |"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())

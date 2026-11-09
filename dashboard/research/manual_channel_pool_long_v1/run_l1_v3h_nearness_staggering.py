#!/usr/bin/env python3
"""L1 V3H — V3G re-run with current drawings (READ-ONLY, same rules)."""

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
ROOT = PROBE_DIR.parents[1]
DRAWINGS_PATH = ROOT / "research_charts" / "user_data" / "drawings.json"
V3G_REF_SHA256 = "e062a5a0b6802363cdf9048cba26b460344cef711ed93133c909ade466252ce8"

if str(PROBE_DIR) not in sys.path:
    sys.path.insert(0, str(PROBE_DIR))

import pilot_engine as pe
import run_l1_pilot as l1
import run_l1_v3g_nearness_staggering as v3g
from config import (
    L1_V3E_VARIANT_A,
    L1_V3E_VARIANT_B,
    L1_V3E_VARIANT_C,
    PILOT_ALLOWED_LOWER_TREND_IDS,
    UPPER_MAIN_CHANNEL_LINE_ID,
)
from line_geometry import utc_ts

LINE_EXTEND_UNTIL = utc_ts(datetime(2026, 9, 9, 17, 30, 0, tzinfo=timezone.utc))
from pool_d2_metrics import apply_d2_gate_to_pending, enrich_d2_channel_filter_fields

OUT_DIR = PROBE_DIR
SEP_START = utc_ts(datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc))


def _parse_utc(s: str) -> int | None:
    if not s:
        return None
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc).timestamp())


def _in_sep(ts: int | None) -> bool:
    return ts is not None and SEP_START <= ts < v3g.V3G_WINDOW_END


def _q1_bands(row: dict) -> str:
    qb = str(row.get("d2_quarter_bands") or "0/0/0/0")
    return qb.split("/")[0]


def _line_activity_september(lines_by_id: dict) -> dict[str, Any]:
    samples = [
        SEP_START,
        LINE_EXTEND_UNTIL,
        utc_ts(datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)),
        v3g.V3G_WINDOW_END - 300,
    ]
    per_line = []
    for did in sorted(PILOT_ALLOWED_LOWER_TREND_IDS | {UPPER_MAIN_CHANNEL_LINE_ID}):
        ln = lines_by_id.get(did)
        if not ln:
            per_line.append({"drawing_id": did, "missing": True})
            continue
        snaps = []
        for t in samples:
            snaps.append(
                {
                    "bar_utc": l1.fmt_utc(t),
                    "active": ln.active_for_candle(float(t)),
                    "anchor_status": (
                        ln.anchor_status(float(t)) if ln.drawing_type == "trend" else "HLINE"
                    ),
                    "extend_right": ln.extend_right,
                }
            )
        per_line.append({"drawing_id": did, "september_samples": snaps})
    active_count = sum(
        1
        for pl in per_line
        if not pl.get("missing")
        and any(s["active"] for s in pl.get("september_samples", []))
    )
    return {"lines": per_line, "active_in_september_count": active_count}


def _september_touch_events(ctx: dict) -> int:
    candles = ctx["candles"]
    n = 0
    for ev in ctx.get("all_events", []):
        if ev.line.drawing_id not in PILOT_ALLOWED_LOWER_TREND_IDS:
            continue
        t = int(candles[ev.start_idx]["time"])
        if _in_sep(t):
            n += 1
    return n


def _trade_lookup(trades: list[dict]) -> dict[str, dict]:
    return {t["touch_event_id"]: t for t in trades}


def _variant_outcome(trades: list[dict], eid: str) -> tuple[str, str, str]:
    t = _trade_lookup(trades).get(eid, {})
    if t.get("exit_kind") not in ("NO_TRADE", "", None):
        return "EXECUTED", str(t.get("exit_kind")), str(t.get("reject_reason") or "")
    reason = t.get("reject_reason") or ""
    return "NO_EXEC", "", reason


def _september_rows(
    pending: list[dict],
    tr_a: list[dict],
    tr_b: list[dict],
    tr_c: list[dict],
    variants_filter,
) -> list[dict]:
    rows: list[dict] = []
    for row in pending:
        t_touch = _parse_utc(str(row.get("touch_utc") or ""))
        t_entry = _parse_utc(str(row.get("entry_utc") or ""))
        if not (_in_sep(t_touch) or _in_sep(t_entry)):
            continue
        eid = row["touch_event_id"]
        fa, _, ra = _variant_outcome(tr_a, eid)
        fb, _, rb = _variant_outcome(tr_b, eid)
        fc, _, rc = _variant_outcome(tr_c, eid)
        filt = variants_filter(row)
        c1_ok = (
            row.get("long_trigger_allowed") == "yes"
            and bool(row.get("entry_utc"))
            and row.get("reject_reason") != "NO_C1_CONFIRM_AFTER_FIRST_TOUCH"
        )
        global_block = any(
            x == "GLOBAL_MAX_ONE_OPEN_LONG"
            for x in (ra, rb, rc, row.get("reject_reason") or "")
        )
        rows.append(
            {
                "touch_utc": row.get("touch_utc", ""),
                "entry_utc": row.get("entry_utc", ""),
                "touch_drawing_id": row.get("touch_drawing_id", ""),
                "long_trigger_allowed": row.get("long_trigger_allowed", ""),
                "c1_ok": "yes" if c1_ok else "no",
                "filter_A": filt["A"],
                "filter_B": filt["B"],
                "filter_C": filt["C"],
                "outcome_A": fa,
                "outcome_B": fb,
                "outcome_C": fc,
                "reject_A": ra or filt["A"],
                "reject_B": rb or filt["B"],
                "reject_C": rc or filt["C"],
                "nearest_d2_pct": row.get("d2_first_in_channel_pct", ""),
                "bands_in_section_0_25": _q1_bands(row),
                "channel_room_pct": row.get("channel_room_pct", ""),
                "global_max_one_open_long": "yes" if global_block else "no",
            }
        )
    rows.sort(key=lambda r: (r["touch_utc"], r["touch_drawing_id"]))
    return rows


def _executed_sep(trades: list[dict]) -> list[dict]:
    out = []
    for t in v3g._executed(trades):
        ent = _parse_utc(str(t.get("entry_utc") or ""))
        ex = v3g._exit_ts(t)
        if _in_sep(ent) or _in_sep(ex):
            out.append(t)
    return out


def _bottleneck_summary(sep_rows: list[dict], sep_touches: int) -> list[str]:
    if sep_touches == 0:
        return ["Keine Allowlist-Touches im September (touch-start 01.09.–20.09.)."]
    c1 = [r for r in sep_rows if r["c1_ok"] == "yes"]
    if not c1:
        reasons = {}
        for r in sep_rows:
            key = r["reject_A"] or r.get("long_trigger_allowed") or "no_trigger"
            reasons[key] = reasons.get(key, 0) + 1
        return [
            f"Touches vorhanden ({sep_touches}), aber **kein C1-Entry** im September.",
            f"Gründe (Stichprobe): {reasons}",
        ]
    admit_a = sum(1 for r in sep_rows if r["filter_A"] == "ADMIT")
    if admit_a == 0:
        return ["C1-Kandidaten vorhanden, aber **keine Variante-A-Freigabe** (Filter/Room/D2/Pick)."]
    exec_a = sum(1 for r in sep_rows if r["outcome_A"] == "EXECUTED")
    if exec_a == 0:
        blk = [r for r in sep_rows if r["global_max_one_open_long"] == "yes"]
        if blk:
            return ["Freigabe möglich, aber **GLOBAL_MAX_ONE_OPEN_LONG** blockiert Ausführung."]
        return ["Kandidaten freigegeben, aber **keine Ausführung** (Pick/Engine — siehe reject-Spalten)."]
    return [f"{exec_a} ausgeführte September-Trades in Variante A."]


def main() -> int:
    l1._bootstrap_import_paths()
    text = DRAWINGS_PATH.read_text(encoding="utf-8") if DRAWINGS_PATH.is_file() else ""
    sha = hashlib.sha256(text.encode()).hexdigest() if text else ""
    manifest = v3g._drawings_manifest()

    saved = (l1.WINDOW_START, l1.WINDOW_END, l1.CANDLE_LOAD_LIMIT)
    l1.WINDOW_START = v3g.V3G_WINDOW_START
    l1.WINDOW_END = v3g.V3G_WINDOW_END
    l1.CANDLE_LOAD_LIMIT = 10000
    try:
        ctx = l1.build_pilot_context()
    finally:
        l1.WINDOW_START, l1.WINDOW_END, l1.CANDLE_LOAD_LIMIT = saved

    if ctx is None:
        return 2

    cand_audit = v3g._audit_candles(ctx["candles"], v3g.V3G_WINDOW_START, v3g.V3G_WINDOW_END)
    mod = sys.modules[l1.__name__]
    lines = list(ctx["lines_by_id"].values())
    pending = copy.deepcopy(ctx["pending"])
    apply_d2_gate_to_pending(pending, lines, l1_module=mod, apply_provisional_research_filter=False)
    pe.enrich_pending_metrics(pending, ctx["upper_line"], mod)
    enrich_d2_channel_filter_fields(pending, ctx["upper_line"], mod)

    variants = {"A": L1_V3E_VARIANT_A, "B": L1_V3E_VARIANT_B, "C": L1_V3E_VARIANT_C}
    tr: dict[str, list[dict]] = {}
    summary: dict[str, dict] = {}
    for code, var in variants.items():
        tr[code] = pe.execute_variant(
            var, copy.deepcopy(pending), ctx["candles"], upper_line=ctx["upper_line"], l1_module=mod
        )
        sm = v3g._summary_metrics(tr[code], ctx["touch_stats"])
        sm["admitted"] = v3g._admitted_candidates(
            var, pending, ctx["candles"], upper_line=ctx["upper_line"], l1_module=mod
        )
        summary[code] = sm
        v3g._write_trades_csv(OUT_DIR / f"l1_v3h_variant_{code.lower()}_trades.csv", tr[code])

    cand_rows = []
    filt_fn = lambda row: v3g._filter_decisions(row, variants)
    for row in pending:
        if not row.get("entry_utc"):
            continue
        base = {k: v for k, v in row.items() if not str(k).startswith("_")}
        dec = filt_fn(row)
        base["filter_A"], base["filter_B"], base["filter_C"] = dec["A"], dec["B"], dec["C"]
        cand_rows.append(base)
    with (OUT_DIR / "l1_v3h_candidates.csv").open("w", newline="", encoding="utf-8") as f:
        if cand_rows:
            w = csv.DictWriter(f, fieldnames=sorted({k for r in cand_rows for k in r}))
            w.writeheader()
            w.writerows(cand_rows)

    sep_geom = _line_activity_september(ctx["lines_by_id"])
    sep_touches = _september_touch_events(ctx)
    sep_pending_touch = [r for r in pending if _in_sep(_parse_utc(str(r.get("touch_utc") or "")))]
    sep_c1 = [r for r in pending if _in_sep(_parse_utc(str(r.get("entry_utc") or "")))]
    sep_table = _september_rows(pending, tr["A"], tr["B"], tr["C"], filt_fn)

    with (OUT_DIR / "l1_v3h_september_candidates.csv").open("w", newline="", encoding="utf-8") as f:
        if sep_table:
            w = csv.DictWriter(f, fieldnames=list(sep_table[0].keys()))
            w.writeheader()
            w.writerows(sep_table)

    sep_exec = {c: _executed_sep(tr[c]) for c in ("A", "B", "C")}
    v3g_geom_old = json.loads(
        (OUT_DIR / "l1_v3g_data_coverage_report.json").read_text(encoding="utf-8")
    ).get("line_geometry", {})
    old_active = sum(
        1
        for ln in v3g_geom_old.get("lines", [])
        if not ln.get("missing")
        and any(s.get("active") for s in ln.get("samples", []) if "2026-09" in s.get("bar_utc", ""))
    )

    coverage = {
        "drawings_sha256": sha,
        "v3g_ref_sha256": V3G_REF_SHA256,
        "drawings_changed_vs_v3g": sha != V3G_REF_SHA256,
        "drawing_count": manifest["drawing_count"],
        "v3g_drawing_count": 24,
        "line_extended_until_utc": l1.fmt_utc(LINE_EXTEND_UNTIL),
        "september_line_activity": sep_geom,
        "candles": cand_audit,
        "september_stats": {
            "touch_events_allowlist": sep_touches,
            "pilot_rows_touch_in_sep": len(sep_pending_touch),
            "c1_entries_in_sep": len(sep_c1),
            "executed_A": len(sep_exec["A"]),
            "executed_B": len(sep_exec["B"]),
            "executed_C": len(sep_exec["C"]),
        },
    }
    (OUT_DIR / "l1_v3h_data_coverage_report.json").write_text(
        json.dumps(coverage, indent=2, default=str) + "\n", encoding="utf-8"
    )

    md = [
        "# L1 V3H — Re-Run aktueller Zeichnungsstand",
        "",
        "## 1. Reproduzierbarkeit",
        "",
        f"- drawings SHA256: `{sha}`",
        f"- Anzahl Zeichnungen: **{manifest['drawing_count']}** (V3G: 24, V3F-Referenz: 19)",
        f"- Geändert gegenüber V3G (`{V3G_REF_SHA256[:16]}…`): **{'ja' if sha != V3G_REF_SHA256 else 'nein'}**",
        f"- Linien **aktiv im September** (Allowlist+Kanal, mind. ein Sample aktiv): **{sep_geom['active_in_september_count']}** "
        f"(V3G-Stichprobe Sept.: ~{old_active} mit aktivem Sept.-Bar)",
        f"- Kerzen: {cand_audit['loaded']}/{cand_audit['expected']}, Lücken={cand_audit['gap_count']}",
        "",
        "## 2. Gesamtergebnis",
        "",
        "| | A | B | C |",
        "| --- | ---: | ---: | ---: |",
        f"| Ausgeführt | {summary['A']['executed']} | {summary['B']['executed']} | {summary['C']['executed']} |",
        f"| Gewinner | {summary['A']['win_count']} | {summary['B']['win_count']} | {summary['C']['win_count']} |",
        f"| Stop-Loss | {summary['A']['loss_count']} | {summary['B']['loss_count']} | {summary['C']['loss_count']} |",
        f"| **Gesamtbrutto %** | **{summary['A']['gross_realized_sum']:.4f}** | **{summary['B']['gross_realized_sum']:.4f}** | "
        f"**{summary['C']['gross_realized_sum']:.4f}** |",
        f"| Offen am Ende | {summary['A']['open_count']} | {summary['B']['open_count']} | {summary['C']['open_count']} |",
        "",
        "## 3. September 01.09.–20.09.",
        "",
        "| Kennzahl | A | B | C |",
        "| --- | ---: | ---: | ---: |",
        f"| Touch-Events (Allowlist) | {sep_touches} | = | = |",
        f"| C1-Entries (entry_utc im Sep.) | {len(sep_c1)} | = | = |",
        f"| Ausgeführt (Entry oder Exit im Sep.) | {len(sep_exec['A'])} | {len(sep_exec['B'])} | {len(sep_exec['C'])} |",
        "",
        "### Engpass",
        "",
        *[f"- {line}" for line in _bottleneck_summary(sep_table, sep_touches)],
        "",
        "## 4. September-Kandidaten",
        "",
        "Vollständige CSV: `l1_v3h_september_candidates.csv`",
        "",
    ]
    if sep_table:
        md.append("| touch_utc | entry_utc | drawing | c1 | A | B | C | near_d2% | Q1 | room% | 1-long |")
        md.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for r in sep_table[:40]:
            md.append(
                f"| {r['touch_utc']} | {r['entry_utc'] or '—'} | {r['touch_drawing_id']} | {r['c1_ok']} | "
                f"{r['filter_A']} | {r['filter_B']} | {r['filter_C']} | {r['nearest_d2_pct']} | "
                f"{r['bands_in_section_0_25']} | {r['channel_room_pct']} | {r['global_max_one_open_long']} |"
            )
        if len(sep_table) > 40:
            md.append(f"_… +{len(sep_table)-40} Zeilen in CSV._")
    else:
        md.append("_Keine September-Zeilen in pending._")

    md.extend(["", "## 5. September-Trades", ""])
    any_sep = False
    for code in ("A", "B", "C"):
        if sep_exec[code]:
            any_sep = True
            for t in sep_exec[code]:
                md.append(
                    f"- **{code}** {t['entry_utc']} {t['touch_drawing_id']}: {t['exit_kind']} "
                    f"exit {t['exit_utc']} PnL={t['gross_pnl_pct']}%"
                )
    if not any_sep:
        md.append("_Keine ausgeführten Trades mit Entry/Exit im September._")

    md.extend(["", "## Fazit", ""])
    if any_sep:
        md.append("1. **Ja** — neue September-Setups mit erweiterten Linien.")
    else:
        md.append(
            "1. **Nein** — trotz aktualisierter Zeichnungen (bis 09.09. 17:30) **keine** neuen September-Trades."
        )
        md.append(
            f"2. Engpass: Touches={sep_touches}, C1-Entries={len(sep_c1)} — siehe Engpass-Abschnitt und CSV."
        )

    (OUT_DIR / "l1_v3h_comparison_report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"V3H sha={sha[:16]} sep_touches={sep_touches} c1={len(sep_c1)} exec_A={len(sep_exec['A'])}")
    print(
        f"gross A={summary['A']['gross_realized_sum']:.4f} B={summary['B']['gross_realized_sum']:.4f} "
        f"C={summary['C']['gross_realized_sum']:.4f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

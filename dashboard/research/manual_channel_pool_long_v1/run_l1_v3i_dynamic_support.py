#!/usr/bin/env python3
"""L1 V3I — OLD allowlist vs dynamic support trends (A/B/C unchanged)."""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROBE_DIR = Path(__file__).resolve().parent
DRAWINGS_PATH = PROBE_DIR.parents[1] / "research_charts" / "user_data" / "drawings.json"
V3H_SHA = "d9a668c028ad13b529f454c7e904034572e13e9671a577f1c66cf46b69fd7743"

if str(PROBE_DIR) not in sys.path:
    sys.path.insert(0, str(PROBE_DIR))

import pilot_engine as pe
import run_l1_pilot as l1
import run_l1_v3g_nearness_staggering as v3g
from config import L1_V3E_VARIANT_A, L1_V3E_VARIANT_B, L1_V3E_VARIANT_C
from line_geometry import utc_ts
from pool_d2_metrics import apply_d2_gate_to_pending, enrich_d2_channel_filter_fields
from support_line_selection import (
    LINE_SELECTION_DYNAMIC,
    LINE_SELECTION_OLD,
    line_anchor_record,
    line_causality_at_touch,
)

OUT_DIR = PROBE_DIR
SEP_START = utc_ts(datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc))
VARIANTS = {"A": L1_V3E_VARIANT_A, "B": L1_V3E_VARIANT_B, "C": L1_V3E_VARIANT_C}


def _parse_utc(s: str) -> int | None:
    if not s:
        return None
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc).timestamp())


def _in_sep(ts: int | None) -> bool:
    return ts is not None and SEP_START <= ts < v3g.V3G_WINDOW_END


def _run_mode(mode: str) -> dict[str, Any]:
    saved = (l1.WINDOW_START, l1.WINDOW_END, l1.CANDLE_LOAD_LIMIT)
    l1.WINDOW_START = v3g.V3G_WINDOW_START
    l1.WINDOW_END = v3g.V3G_WINDOW_END
    l1.CANDLE_LOAD_LIMIT = 10000
    try:
        ctx = l1.build_pilot_context(line_selection_mode=mode)
    finally:
        l1.WINDOW_START, l1.WINDOW_END, l1.CANDLE_LOAD_LIMIT = saved
    if ctx is None:
        raise RuntimeError("build_pilot_context failed")
    mod = sys.modules[l1.__name__]
    lines = list(ctx["lines_by_id"].values())
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


def _sep_stats(ctx: dict, pending: list[dict], summary: dict, trades: dict) -> dict[str, Any]:
    touches = sum(
        1
        for row in pending
        if _in_sep(_parse_utc(str(row.get("touch_utc") or "")))
        and row.get("long_trigger_allowed") == "yes"
    )
    c1 = sum(1 for row in pending if _in_sep(_parse_utc(str(row.get("entry_utc") or ""))))
    admitted = {k: summary[k]["admitted"] for k in "ABC"}
    ex = {k: len([t for t in v3g._executed(trades[k]) if _in_sep(_parse_utc(t.get("entry_utc", "")))]) for k in "ABC"}
    return {
        "support_touches": touches,
        "c1_entries": c1,
        "admitted_A": admitted["A"],
        "admitted_B": admitted["B"],
        "admitted_C": admitted["C"],
        "executed": ex,
        "gross_realized_A": summary["A"]["gross_realized_sum"],
    }


def main() -> int:
    l1._bootstrap_import_paths()
    text = DRAWINGS_PATH.read_text(encoding="utf-8")
    sha = hashlib.sha256(text.encode()).hexdigest()
    payload = json.loads(text)
    snap_path = OUT_DIR / f"l1_v3i_drawings_snapshot_{sha[:16]}.json"
    if not snap_path.is_file():
        snap_path.write_text(text, encoding="utf-8")

    old = _run_mode(LINE_SELECTION_OLD)
    dyn = _run_mode(LINE_SELECTION_DYNAMIC)

    old_rows: list[dict] = []
    for code, tr in old["trades"].items():
        for t in tr:
            r = {k: v for k, v in t.items() if not str(k).startswith("_")}
            r["line_mode"] = "OLD_ALLOWLIST"
            r["variant"] = code
            old_rows.append(r)
    with (OUT_DIR / "l1_v3i_old_allowlist_trades.csv").open("w", newline="", encoding="utf-8") as f:
        if old_rows:
            w = csv.DictWriter(f, fieldnames=sorted({k for r in old_rows for k in r}))
            w.writeheader()
            w.writerows(old_rows)

    for code in ("A", "B", "C"):
        v3g._write_trades_csv(
            OUT_DIR / f"l1_v3i_dynamic_variant_{code.lower()}_trades.csv",
            dyn["trades"][code],
        )

    dyn_lines = []
    for ln in old["ctx"]["lines"]:
        if ln.drawing_type == "trend" and ln.drawing_id != l1.UPPER_MAIN_CHANNEL_LINE_ID:
            rec = line_anchor_record(ln)
            rec["in_old_allowlist"] = ln.drawing_id in l1.PILOT_ALLOWED_LOWER_TREND_IDS
            stats = next(
                (s for s in _line_stats(dyn["pending"], dyn["trades"]["A"]) if s["drawing_id"] == ln.drawing_id),
                {},
            )
            rec.update(stats)
            dyn_lines.append(rec)
    with (OUT_DIR / "l1_v3i_dynamic_support_lines.csv").open("w", newline="", encoding="utf-8") as f:
        if dyn_lines:
            w = csv.DictWriter(f, fieldnames=sorted({k for r in dyn_lines for k in r}))
            w.writeheader()
            w.writerows(dyn_lines)

    cand_rows = []
    for row in dyn["pending"]:
        if not row.get("entry_utc"):
            continue
        base = {k: v for k, v in row.items() if not str(k).startswith("_")}
        dec = v3g._filter_decisions(row, VARIANTS)
        base["filter_A"], base["filter_B"], base["filter_C"] = dec["A"], dec["B"], dec["C"]
        cand_rows.append(base)
    with (OUT_DIR / "l1_v3i_candidates.csv").open("w", newline="", encoding="utf-8") as f:
        if cand_rows:
            w = csv.DictWriter(f, fieldnames=sorted({k for r in cand_rows for k in r}))
            w.writeheader()
            w.writerows(cand_rows)

    sep_old = _sep_stats(old["ctx"], old["pending"], old["summary"], old["trades"])
    sep_dyn = _sep_stats(dyn["ctx"], dyn["pending"], dyn["summary"], dyn["trades"])

    md = [
        "# L1 V3I — Dynamic Support vs OLD Allowlist",
        "",
        f"- drawings SHA256: `{sha}` (count {len(payload.get('drawings') or [])})",
        f"- Snapshot: `{snap_path.name}`",
        f"- Gleich V3H (`{V3H_SHA[:16]}…`): **{'ja' if sha == V3H_SHA else 'nein'}**",
        f"- Fenster: 2026-08-25 .. 2026-09-20 exkl.",
        "",
        "## Vergleichstabelle",
        "",
        "| Modus / Variante | Trades | Gewinne | SL | Gesamtbrutto % |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for mode_name, pack in (("OLD_ALLOWLIST", old), ("DYNAMIC_SUPPORT", dyn)):
        for code in ("A", "B", "C"):
            sm = pack["summary"][code]
            md.append(
                f"| {mode_name} {code} | {sm['executed']} | {sm['win_count']} | {sm['loss_count']} | "
                f"{sm['gross_realized_sum']:.4f} |"
            )

    md.extend(
        [
            "",
            "## September (DYNAMIC_SUPPORT)",
            "",
            f"- Support-Touches (long_trigger_allowed): {sep_dyn['support_touches']}",
            f"- C1-Entries: {sep_dyn['c1_entries']}",
            f"- Zugelassen A/B/C: {sep_dyn['admitted_A']}/{sep_dyn['admitted_B']}/{sep_dyn['admitted_C']}",
            f"- Ausgeführt A/B/C: {sep_dyn['executed']['A']}/{sep_dyn['executed']['B']}/{sep_dyn['executed']['C']}",
            "",
            "## OLD Regressionscheck (vs V3H)",
            "",
            f"- OLD A brutto {old['summary']['A']['gross_realized_sum']:.4f} (ref 7.7204)",
            f"- OLD B/C brutto {old['summary']['B']['gross_realized_sum']:.4f} (ref 8.9204)",
            "",
            "## Antworten §19",
            "",
            "1. **Ja** — im Modus `DYNAMIC_SUPPORT` werden alle geeigneten diagonalen Support-Trendlinien "
            "ohne feste ID-Allowlist geprüft (weiterhin: Support von oben, unter Hauptkanal, kein Hline/Upper-Main).",
            f"2. September C1-Entries dynamisch: **{sep_dyn['c1_entries']}** (OLD: {sep_old['c1_entries']}).",
            f"3. September ausgeführt dynamisch A/B/C: **{sep_dyn['executed']}**.",
            "4. A/B/C-Filterlogik unverändert; Unterschiede nur durch mehr/k weniger Trigger-Linien.",
            "5. Einschränkungen: Kausalität (`drawing_causality`) getrennt von Geometrie; "
            "`extend_right=false` endet am 2. Anker; retrospektive Linien nicht als live-kausal deklarieren.",
            "",
        ]
    )

    new_ids = sorted(
        {
            r["touch_drawing_id"]
            for r in dyn["pending"]
            if r.get("long_trigger_allowed") == "yes"
            and r["touch_drawing_id"] not in l1.PILOT_ALLOWED_LOWER_TREND_IDS
        }
    )
    md.append(f"Neue dynamische Support-IDs (nicht in OLD-Allowlist): {', '.join(new_ids) or '—'}")

    (OUT_DIR / "l1_v3i_dynamic_support_report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(
        f"V3I old A={old['summary']['A']['gross_realized_sum']:.4f} "
        f"dyn A={dyn['summary']['A']['gross_realized_sum']:.4f} sep_c1_dyn={sep_dyn['c1_entries']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

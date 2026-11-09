#!/usr/bin/env python3
"""L1 V3B — canonical D2 gate vs micro proxy; parity + pilot comparison."""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROBE_DIR = Path(__file__).resolve().parent
ROOT = PROBE_DIR.parents[1]
DRAWINGS_PATH = ROOT / "research_charts" / "user_data" / "drawings.json"

if str(PROBE_DIR) not in sys.path:
    sys.path.insert(0, str(PROBE_DIR))

import pilot_engine as pe
import run_l1_pilot as l1
from config import (
    D2_MIN_DEPTH,
    L1_V3B_D2_GATE,
    L1_V3B_D2_RESEARCH,
    L1_V3B_MICRO,
)
from pd1_exact_d2_adapter import (
    D2_COMPUTE_VERSION,
    price_window_for_entry,
    upper_d2_bands_at_close,
)
from pool_d2_metrics import apply_d2_gate_to_pending, analyze_d2_landscape, metrics_to_fields

OUT_DIR = PROBE_DIR
AUDIT_REFS = {
    "2026-08-25 21:15:00 UTC": {"bands": 55, "near_pct": 3.610949330227147},
    "2026-08-26 15:10:00 UTC": {"bands": 69, "near_pct": 1.8948365703458128},
}


def _drawings_manifest() -> dict[str, str]:
    text = DRAWINGS_PATH.read_text(encoding="utf-8") if DRAWINGS_PATH.is_file() else ""
    return {
        "sha256": hashlib.sha256(text.encode()).hexdigest() if text else "",
        "count": str(len(json.loads(text).get("drawings") or []) if text else 0),
    }


def _git_head() -> str:
    try:
        r = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return r.stdout.strip() if r.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _parity_check(pending: list[dict]) -> list[str]:
    lines = []
    for entry_utc, ref in AUDIT_REFS.items():
        row = next((r for r in pending if r.get("entry_utc") == entry_utc), None)
        if not row:
            lines.append(f"- {entry_utc}: **MISSING** candidate row")
            continue
        dc = datetime.strptime(
            row["_pool_decision_close"], "%Y-%m-%d %H:%M:%S UTC"
        ).replace(tzinfo=timezone.utc)
        entry = float(row["entry_price"])
        bands, err = upper_d2_bands_at_close(row.get("_pool_members"), dc, entry, min_depth=D2_MIN_DEPTH)
        if err:
            lines.append(f"- {entry_utc}: **MISMATCH** {err}")
            continue
        near = None
        for b in bands:
            if b.bottom > entry:
                near = (b.bottom - entry) / entry * 100.0
                break
        ok_bands = len(bands) == ref["bands"]
        ok_near = near is not None and abs(near - ref["near_pct"]) < 0.02
        verdict = "MATCH" if ok_bands and ok_near else "MISMATCH"
        lines.append(
            f"- {entry_utc}: {verdict} — bands {len(bands)} (ref {ref['bands']}), "
            f"first D2 above entry {near:.4f}% (ref {ref['near_pct']:.4f}%)"
        )
    return lines


def _executed(trades: list[dict]) -> list[dict]:
    return sorted(
        [t for t in trades if t.get("exit_kind") not in ("NO_TRADE", "")],
        key=lambda t: t.get("entry_utc", ""),
    )


def _trade_tables(trades: list[dict], title: str) -> list[str]:
    ex = _executed(trades)
    wins = [t for t in ex if t.get("exit_kind") in ("TP", "CHANNEL_UPPER")]
    losses = [t for t in ex if t.get("exit_kind") == "SL"]
    out = [f"### {title}", ""]
    if wins:
        out.append("| Entry UTC | Entry | SL | TP@Entry | Exit | Exit UTC | Typ | PnL % |")
        out.append("| --- | --- | --- | --- | --- | --- | --- | ---: |")
        for t in wins:
            out.append(
                f"| {t['entry_utc']} | {t['entry_price']} | {t['sl_price']} | {t['tp_price']} "
                f"| {t['exit_price']} | {t['exit_utc']} | {t['exit_kind']} | {t['gross_pnl_pct']} |"
            )
    else:
        out.append("_Keine Gewinn-Trades._")
    out.extend(["", f"#### Stop-Loss — {title}", ""])
    if losses:
        out.append("| Entry UTC | Entry | SL | TP@Entry | Exit | SL UTC | PnL % |")
        out.append("| --- | --- | --- | --- | --- | --- | ---: |")
        for t in losses:
            out.append(
                f"| {t['entry_utc']} | {t['entry_price']} | {t['sl_price']} | {t['tp_price']} "
                f"| {t['exit_price']} | {t['exit_utc']} | {t['gross_pnl_pct']} |"
            )
    else:
        out.append("_Keine SL-Trades._")
    out.append("")
    return out


def main() -> int:
    l1._bootstrap_import_paths()
    manifest = _drawings_manifest()
    ctx = l1.build_pilot_context()
    if ctx is None:
        return 2

    mod = sys.modules[l1.__name__]
    lines = list(ctx["lines_by_id"].values())
    pending_base = ctx["pending"]

    pe.enrich_pending_metrics(pending_base, ctx["upper_line"], mod)

    parity = _parity_check(pending_base)

    pending_micro = copy.deepcopy(pending_base)
    pending_d2 = copy.deepcopy(pending_base)
    apply_d2_gate_to_pending(pending_d2, lines, l1_module=mod, apply_provisional_research_filter=False)
    pe.enrich_pending_metrics(pending_d2, ctx["upper_line"], mod)

    pending_d2_r = copy.deepcopy(pending_base)
    apply_d2_gate_to_pending(
        pending_d2_r, lines, l1_module=mod, apply_provisional_research_filter=True
    )
    pe.enrich_pending_metrics(pending_d2_r, ctx["upper_line"], mod)

    tr_micro = pe.execute_variant(
        L1_V3B_MICRO, pending_micro, ctx["candles"], upper_line=ctx["upper_line"], l1_module=mod
    )
    tr_d2 = pe.execute_variant(
        L1_V3B_D2_GATE, pending_d2, ctx["candles"], upper_line=ctx["upper_line"], l1_module=mod
    )
    tr_d2r = pe.execute_variant(
        L1_V3B_D2_RESEARCH,
        pending_d2_r,
        ctx["candles"],
        upper_line=ctx["upper_line"],
        l1_module=mod,
    )

    sm = pe.summarize_variant(tr_micro, ctx["touch_stats"])
    sd = pe.summarize_variant(tr_d2, ctx["touch_stats"])
    sdr = pe.summarize_variant(tr_d2r, ctx["touch_stats"])

    candidates_rows = []
    by_micro = {t["touch_event_id"]: t for t in tr_micro}
    by_d2 = {t["touch_event_id"]: t for t in tr_d2}
    for row in pending_d2:
        if row.get("long_trigger_allowed") != "yes" or not row.get("entry_utc"):
            continue
        dc = datetime.strptime(
            row["_pool_decision_close"], "%Y-%m-%d %H:%M:%S UTC"
        ).replace(tzinfo=timezone.utc)
        m, _, _ = analyze_d2_landscape(row.get("_pool_members"), dc, float(row["entry_price"]))
        cand = {k: v for k, v in row.items() if not str(k).startswith("_")}
        cand.update(metrics_to_fields(m))
        cand["micro_variant_exit"] = by_micro.get(row["touch_event_id"], {}).get("exit_kind", "")
        cand["d2_variant_exit"] = by_d2.get(row["touch_event_id"], {}).get("exit_kind", "")
        candidates_rows.append(cand)

    csv_path = OUT_DIR / "l1_v3b_d2_candidates.csv"
    if candidates_rows:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=sorted({k for r in candidates_rows for k in r}))
            w.writeheader()
            w.writerows(candidates_rows)

    # Decision deltas
    deltas = []
    for eid in sorted(set(by_micro) | set(by_d2)):
        a, b = by_micro.get(eid, {}), by_d2.get(eid, {})
        if a.get("exit_kind") == b.get("exit_kind") and a.get("reject_reason") == b.get("reject_reason"):
            continue
        if a.get("exit_kind") not in ("NO_TRADE", "") or b.get("exit_kind") not in ("NO_TRADE", ""):
            deltas.append(
                f"- `{eid}` entry {a.get('entry_utc') or b.get('entry_utc')}: "
                f"micro={a.get('exit_kind')}/{a.get('reject_reason') or '—'} → "
                f"D2={b.get('exit_kind')}/{b.get('reject_reason') or '—'}"
            )

    loss_25 = next((t for t in tr_d2 if t.get("entry_utc") == "2026-08-25 21:15:00 UTC"), {})
    win_26 = next((t for t in tr_d2 if t.get("entry_utc") == "2026-08-26 15:10:00 UTC"), {})

    md = [
        "# L1 V3B — Kanonisches D2-Gate",
        "",
        "## Reproduzierbarkeit",
        "",
        f"- Git: `{_git_head()}`",
        f"- drawings SHA256: `{manifest['sha256']}` ({manifest['count']} Zeichnungen)",
        f"- D2 compute: `{D2_COMPUTE_VERSION}` min_depth={D2_MIN_DEPTH}",
        f"- Preisfenster: entry×0.92 … entry×1.15 (L1 V3A Parität)",
        f"- SSOT: `load_historical_pool_state_at_close` 5m PD1",
        f"- Fenster: 2026-08-25 .. 2026-08-29 exkl.",
        "",
        "## A. Technischer D2-Abgleich (Audit-Referenzen)",
        "",
        *parity,
        "",
        "## B. Kandidaten",
        "",
        "CSV: `l1_v3b_d2_candidates.csv`",
        "",
        "| Entry UTC | channel room % | micro qual | D2 bands | D2 qual | nearest D2 % | D2 gate |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for entry in ("2026-08-25 21:15:00 UTC", "2026-08-26 15:10:00 UTC"):
        rm = next((r for r in pending_micro if r.get("entry_utc") == entry), {})
        rd = next((r for r in pending_d2 if r.get("entry_utc") == entry), {})
        md.append(
            f"| {entry} | {rm.get('channel_room_pct','')} | {rm.get('pool_gate_qualified_count','')} | "
            f"{rd.get('d2_band_count','')} | {rd.get('d2_qualified_count','')} | "
            f"{rd.get('d2_nearest_dist_pct','')} | {rd.get('d2_gate_pass','')} |"
        )
    md.extend(
        [
        "",
        "Micro-Proxy-Zähler (62/74) sind **einzelne Micro-Bänder**; D2-qual (55/69) sind **kanonische Depth≥2-Bänder**.",
        "",
        "## D. Gesamtvergleich",
        "",
        "| Variante | Executed | SL | Ch-TP | Σ Brutto % |",
        "| --- | ---: | ---: | ---: | ---: |",
        f"| Micro-Proxy (V3A) | {sm['executed']} | {sm['sl']} | {sm['tp_channel']} | {sm['pnl_sum']:.4f} |",
        f"| **D2 kanonisch** | {sd['executed']} | {sd['sl']} | {sd['tp_channel']} | {sd['pnl_sum']:.4f} |",
        f"| D2 + Research (nicht kalibriert) | {sdr['executed']} | {sdr['sl']} | {sdr['tp_channel']} | {sdr['pnl_sum']:.4f} |",
        "",
        "### Entscheidungsänderungen Micro → D2",
        "",
        *(deltas or ["_Keine ausgeführten Trade-Unterschiede._"]),
        "",
        ]
    )
    md.extend(_trade_tables(tr_micro, "Micro-Proxy"))
    md.extend(_trade_tables(tr_d2, "Kanonisches D2-Gate"))
    md.extend(
        [
            "## Abschlussfragen",
            "",
            f"1. **Exakt dieselben D2-Bänder wie Charts?** Siehe Parität oben — bei MATCH: ja (gleiche SSOT + Sweep-Port).",
            f"2. **Bandgrenzen/Origin-Tiefe identisch?** `depth` = distinct logical origins im Sweep; Stücktest Parität.",
            f"3. **Kandidatenänderungen:** {len(deltas)} dokumentierte Micro↔D2-Abweichungen.",
            f"4. **25.08. Verlust abgelehnt (D2)?** micro exit={loss_25.get('exit_kind','—')} "
            f"reject={loss_25.get('reject_reason') or '—'}",
        ]
    )
    row25 = next((r for r in pending_d2 if r.get("entry_utc") == "2026-08-25 21:15:00 UTC"), {})
    md.append(
        f"   — D2-Variante: exit={loss_25.get('exit_kind')} "
        f"| d2_gate={row25.get('d2_gate_pass')} "
        f"| qualified_d2={row25.get('d2_qualified_count')}"
    )
    md.append(
        f"5. **26.08. Gewinner zugelassen?** D2 exit={win_26.get('exit_kind')} "
        f"pnl={win_26.get('gross_pnl_pct', '—')}"
    )

    report = OUT_DIR / "l1_v3b_d2_report.md"
    report.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"Wrote {report}")
    print(f"micro={sm['executed']} pnl={sm['pnl_sum']:.4f} | d2={sd['executed']} pnl={sd['pnl_sum']:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

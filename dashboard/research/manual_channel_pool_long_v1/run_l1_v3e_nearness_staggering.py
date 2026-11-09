#!/usr/bin/env python3
"""L1 V3E — D2 nearness + early channel staggering comparison (isolated research)."""

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
    L1_V3E_VARIANT_A,
    L1_V3E_VARIANT_B,
    L1_V3E_VARIANT_C,
    MAX_DISTANCE_TO_FIRST_D2_UPPER_PCT,
    MIN_D2_BANDS_IN_FIRST_CHANNEL_QUARTER,
    PILOT_ALLOWED_LOWER_TREND_IDS,
)
from pd1_exact_d2_adapter import D2_COMPUTE_VERSION
from pool_d2_metrics import (
    apply_d2_gate_to_pending,
    enrich_d2_channel_filter_fields,
)

OUT_DIR = PROBE_DIR

REF_CASES = {
    "2026-08-25 21:15:00 UTC": {"near_pct": 3.610949330227147, "q1": 0},
    "2026-08-26 15:10:00 UTC": {"near_pct": 1.8948365703458128, "q1": 6},
}


def _drawings_manifest() -> dict[str, str]:
    text = DRAWINGS_PATH.read_text(encoding="utf-8") if DRAWINGS_PATH.is_file() else ""
    payload = json.loads(text) if text else {}
    ids = {str(d.get("drawing_id") or d.get("id") or "") for d in payload.get("drawings") or []}
    allowed_present = sorted(i for i in PILOT_ALLOWED_LOWER_TREND_IDS if i in ids)
    missing = sorted(i for i in PILOT_ALLOWED_LOWER_TREND_IDS if i not in ids)
    return {
        "sha256": hashlib.sha256(text.encode()).hexdigest() if text else "",
        "count": str(len(payload.get("drawings") or [])),
        "allowlist_present": ", ".join(allowed_present),
        "allowlist_missing": ", ".join(missing),
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


def _executed(trades: list[dict]) -> list[dict]:
    return [t for t in trades if t.get("exit_kind") not in ("NO_TRADE", "")]


def _write_trades_csv(path: Path, trades: list[dict]) -> None:
    rows = [{k: v for k, v in t.items() if not str(k).startswith("_")} for t in trades]
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=sorted({k for r in rows for k in r}))
        w.writeheader()
        w.writerows(rows)


def _pnl_breakdown(trades: list[dict]) -> dict[str, float]:
    ex = _executed(trades)
    wins = [float(t["gross_pnl_pct"]) for t in ex if t.get("exit_kind") in ("TP", "CHANNEL_UPPER")]
    losses = [float(t["gross_pnl_pct"]) for t in ex if t.get("exit_kind") == "SL"]
    return {
        "wins_sum": sum(wins),
        "losses_sum": sum(losses),
        "gross_sum": sum(wins) + sum(losses),
        "win_count": len(wins),
        "loss_count": len(losses),
    }


def _admitted_candidates(
    variant,
    pending: list[dict],
    candles: list[dict],
    *,
    upper_line,
    l1_module: Any,
) -> int:
    channel_line_at_bar = l1_module.channel_line_at_bar

    def upper_px(candle: dict) -> float | None:
        return channel_line_at_bar(upper_line, candle)

    cycle = pe.ChannelCycleState()
    n = 0
    for row in sorted(
        pending,
        key=lambda r: (
            int(r["_entry_idx"]) if r.get("_entry_idx") not in (None, "") else 10**9,
            r["touch_event_id"],
        ),
    ):
        if row.get("long_trigger_allowed") != "yes" or not row.get("_entry_idx"):
            continue
        entry_idx = int(row["_entry_idx"])
        tsi = row.get("_touch_start_idx")
        if tsi is not None:
            cycle.maybe_reset_at_touch_start(candles, int(tsi), upper_px)
        cycle.advance_to(candles, entry_idx - 1, upper_px)
        entry = float(row["entry_price"])
        main_upper = row.get("_main_upper_at_entry")
        eff = row.get("_effective_tp")
        reason = pe.apply_variant_entry_filters(variant, entry, main_upper, eff, cycle, row=row)
        if reason:
            continue
        if row.get("_pick_ok"):
            n += 1
    return n


def _filter_decisions(row: dict, variants: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for code, var in variants.items():
        if row.get("long_trigger_allowed") != "yes" or not row.get("_entry_idx"):
            out[code] = row.get("reject_reason") or "NOT_LONG_TRIGGER"
            continue
        if not row.get("_d2_gate_ok"):
            out[code] = row.get("d2_gate_reason") or "D2_GATE_FAIL"
            continue
        entry = float(row["entry_price"])
        main_upper = row.get("_main_upper_at_entry")
        from channel_cycle import ChannelCycleState

        cycle = ChannelCycleState()
        eff = row.get("_effective_tp")
        reason = pe.apply_variant_entry_filters(var, entry, main_upper, eff, cycle, row=row)
        if reason:
            out[code] = reason
        elif not row.get("_pick_ok"):
            out[code] = row.get("reject_reason") or "PICK_FAIL"
        else:
            out[code] = "ADMIT"
    return out


def _trade_tables(trades: list[dict], title: str) -> list[str]:
    ex = sorted(_executed(trades), key=lambda t: t.get("entry_utc", ""))
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
        out.append(
            "| Entry UTC | Entry | SL | TP@Entry | Exit | SL UTC | drawing_id | PnL % |"
        )
        out.append("| --- | --- | --- | --- | --- | --- | --- | ---: |")
        for t in losses:
            out.append(
                f"| {t['entry_utc']} | {t['entry_price']} | {t['sl_price']} | {t['tp_price']} "
                f"| {t['exit_price']} | {t['exit_utc']} | {t['touch_drawing_id']} | {t['gross_pnl_pct']} |"
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

    pending = copy.deepcopy(ctx["pending"])
    apply_d2_gate_to_pending(pending, lines, l1_module=mod, apply_provisional_research_filter=False)
    pe.enrich_pending_metrics(pending, ctx["upper_line"], mod)
    enrich_d2_channel_filter_fields(pending, ctx["upper_line"], mod)

    variants = {
        "A": L1_V3E_VARIANT_A,
        "B": L1_V3E_VARIANT_B,
        "C": L1_V3E_VARIANT_C,
    }
    tr: dict[str, list[dict]] = {}
    summary: dict[str, dict] = {}
    for code, var in variants.items():
        tr[code] = pe.execute_variant(
            var, copy.deepcopy(pending), ctx["candles"], upper_line=ctx["upper_line"], l1_module=mod
        )
        sm = pe.summarize_variant(tr[code], ctx["touch_stats"])
        pnl = _pnl_breakdown(tr[code])
        sm["admitted"] = _admitted_candidates(
            var, pending, ctx["candles"], upper_line=ctx["upper_line"], l1_module=mod
        )
        sm.update(pnl)
        sm["reject_first_d2"] = sm["rejects"].get("REJECTED_FIRST_D2_TOO_FAR", 0)
        sm["reject_stagger"] = sm["rejects"].get("REJECTED_NO_EARLY_D2_STAGGERING", 0)
        summary[code] = sm
        _write_trades_csv(OUT_DIR / f"l1_v3e_variant_{code.lower()}_trades.csv", tr[code])

    cand_rows: list[dict] = []
    for row in pending:
        if not row.get("entry_utc"):
            continue
        base = {k: v for k, v in row.items() if not str(k).startswith("_")}
        decisions = _filter_decisions(row, variants)
        base["filter_A"] = decisions["A"]
        base["filter_B"] = decisions["B"]
        base["filter_C"] = decisions["C"]
        cand_rows.append(base)
    with (OUT_DIR / "l1_v3e_candidates.csv").open("w", newline="", encoding="utf-8") as f:
        if cand_rows:
            w = csv.DictWriter(f, fieldnames=sorted({k for r in cand_rows for k in r}))
            w.writeheader()
            w.writerows(cand_rows)

    parity_lines = []
    for entry_utc, ref in REF_CASES.items():
        row = next((r for r in pending if r.get("entry_utc") == entry_utc), None)
        if not row:
            parity_lines.append(f"- {entry_utc}: **MISSING**")
            continue
        near = float(row.get("d2_first_in_channel_pct") or 0)
        q1 = int(str(row.get("d2_quarter_bands") or "0/0/0/0").split("/")[0])
        ok = abs(near - ref["near_pct"]) < 0.02 and q1 == ref["q1"]
        parity_lines.append(
            f"- {entry_utc}: {'MATCH' if ok else 'CHECK'} — near {near:.4f}% (ref {ref['near_pct']:.4f}%), "
            f"Q1 bands {q1} (ref {ref['q1']})"
        )

    ref_checks = []
    for entry, label in (
        ("2026-08-25 21:15:00 UTC", "A loss"),
        ("2026-08-26 15:10:00 UTC", "B win"),
    ):
        for code in ("B", "C"):
            t = next((x for x in tr[code] if x.get("entry_utc") == entry), {})
            ref_checks.append(
                f"- {label} @ {entry}, Variante {code}: exit={t.get('exit_kind','—')} "
                f"reject={t.get('reject_reason') or '—'}"
            )

    identical_bc = all(
        t.get("exit_kind") == tr["B"][i].get("exit_kind")
        and t.get("reject_reason") == tr["B"][i].get("reject_reason")
        for i, t in enumerate(tr["C"])
    )

    md = [
        "# L1 V3E — D2-Nähe + frühe Staffelung",
        "",
        "## Reproduzierbarkeit",
        "",
        f"- Git: `{_git_head()}`",
        f"- drawings SHA256: `{manifest['sha256']}` ({manifest['count']} Zeichnungen)",
        f"- Allowlist vorhanden: {manifest['allowlist_present']}",
        f"- Allowlist fehlt: {manifest['allowlist_missing'] or '—'}",
        f"- D2: `{D2_COMPUTE_VERSION}` depth≥{D2_MIN_DEPTH}",
        f"- MAX_DISTANCE_TO_FIRST_D2_UPPER_PCT = **{MAX_DISTANCE_TO_FIRST_D2_UPPER_PCT:.2f}**",
        f"- MIN_D2_BANDS_IN_FIRST_CHANNEL_QUARTER = **{MIN_D2_BANDS_IN_FIRST_CHANNEL_QUARTER}**",
        f"- Fenster: 2026-08-25 .. 2026-08-29 exkl. (identisch für A/B/C)",
        "",
        "## Referenz-Parität (V3D)",
        "",
        *parity_lines,
        "",
        "## Vergleichstabelle",
        "",
        "| Kennzahl | A (Baseline D2) | B (+Nähe) | C (+Staffelung) |",
        "| --- | ---: | ---: | ---: |",
        f"| Touch-Ereignisse | {summary['A']['touch_events']} | same | same |",
        f"| Long-Trigger erlaubt | {summary['A']['long_triggers_allowed']} | same | same |",
        f"| Zugelassene Kandidaten (ohne 1-Long-Limit) | {summary['A']['admitted']} | {summary['B']['admitted']} | {summary['C']['admitted']} |",
        f"| Ausgeführt | {summary['A']['executed']} | {summary['B']['executed']} | {summary['C']['executed']} |",
        f"| Gewinner | {summary['A']['win_count']} | {summary['B']['win_count']} | {summary['C']['win_count']} |",
        f"| Stop-Loss | {summary['A']['sl']} | {summary['B']['sl']} | {summary['C']['sl']} |",
        f"| Kanal-TP | {summary['A']['tp_channel']} | {summary['B']['tp_channel']} | {summary['C']['tp_channel']} |",
        f"| Pool-TP | {summary['A']['tp_pool']} | {summary['B']['tp_pool']} | {summary['C']['tp_pool']} |",
        f"| Offen am Fensterende | {summary['A']['open_end']} | {summary['B']['open_end']} | {summary['C']['open_end']} |",
        f"| Σ Gewinne % | {summary['A']['wins_sum']:.4f} | {summary['B']['wins_sum']:.4f} | {summary['C']['wins_sum']:.4f} |",
        f"| Σ Verluste % | {summary['A']['losses_sum']:.4f} | {summary['B']['losses_sum']:.4f} | {summary['C']['losses_sum']:.4f} |",
        f"| Σ Brutto % | {summary['A']['gross_sum']:.4f} | {summary['B']['gross_sum']:.4f} | {summary['C']['gross_sum']:.4f} |",
        f"| REJECTED_FIRST_D2_TOO_FAR | 0 | {summary['B']['reject_first_d2']} | {summary['C']['reject_first_d2']} |",
        f"| REJECTED_NO_EARLY_D2_STAGGERING | 0 | 0 | {summary['C']['reject_stagger']} |",
        "",
        "### Referenzfälle B/C",
        "",
        *ref_checks,
        "",
    ]
    for code, label in (
        ("A", "Variante A"),
        ("B", "Variante B"),
        ("C", "Variante C"),
    ):
        md.extend(_trade_tables(tr[code], label))

    blocked_b = [
        t
        for t in tr["A"]
        if t.get("exit_kind") not in ("NO_TRADE", "")
        and next((x for x in tr["B"] if x["touch_event_id"] == t["touch_event_id"]), {}).get("exit_kind")
        in ("NO_TRADE", "")
    ]
    md.extend(
        [
            "## Auswertung (§10)",
            "",
            f"1. **Verhindert Nähe den 25.08.-Verlust?** "
            f"{'Ja' if any(t.get('entry_utc')=='2026-08-25 21:15:00 UTC' and t.get('exit_kind')=='NO_TRADE' for t in tr['B']) else 'Nein'} "
            f"(B: {next((t for t in tr['B'] if t.get('entry_utc')=='2026-08-25 21:15:00 UTC'), {}).get('reject_reason','—')}).",
            f"2. **Bleibt 26.08.-Gewinner?** "
            f"{'Ja' if next((t for t in tr['B'] if t.get('entry_utc')=='2026-08-26 15:10:00 UTC'), {}).get('exit_kind') not in ('NO_TRADE','') else 'Nein'}.",
            f"3. **Staffelung vs. nur Nähe:** {'Kein Unterschied B/C' if identical_bc else 'Unterschied vorhanden'} "
            f"(executed B={summary['B']['executed']} C={summary['C']['executed']}).",
            f"4. **Weitere Blockierungen:** B blockiert {summary['B']['reject_first_d2']} Kandidatenzeilen; "
            f"C zusätzlich {summary['C']['reject_stagger']} wegen Staffelung.",
            f"5. **Neue Trades durch frühere Ablehnung?** Vergleich executed A={summary['A']['executed']} "
            f"B={summary['B']['executed']} C={summary['C']['executed']} — chronologische Simulation.",
            f"6. **A vs B vs C identisch?** A≠B wenn executed oder rejects abweichen; B≠C: {not identical_bc}.",
            "7. **Gegen die Hypothese:** August-Fenster ist nicht out-of-sample; wenige Trades; "
            "fehlende Allowlist-Linie reduziert Kandidaten; hohe Kanal-TPs hängen weiter an Geometrie, nicht nur D2-Landschaft.",
            "",
            "## Verhinderte / neue Trades (A → B)",
            "",
        ]
    )
    if blocked_b:
        for t in blocked_b:
            tb = next(x for x in tr["B"] if x["touch_event_id"] == t["touch_event_id"])
            md.append(
                f"- `{t['touch_event_id']}` {t.get('entry_utc')}: A={t.get('exit_kind')} → "
                f"B={tb.get('exit_kind')} ({tb.get('reject_reason') or '—'})"
            )
    else:
        md.append("_Keine zuvor ausgeführten A-Trades nur durch B gestoppt._")

    (OUT_DIR / "l1_v3e_nearness_staggering_report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"Wrote l1_v3e_nearness_staggering_report.md")
    print(
        f"A exec={summary['A']['executed']} pnl={summary['A']['gross_sum']:.4f} | "
        f"B exec={summary['B']['executed']} pnl={summary['B']['gross_sum']:.4f} | "
        f"C exec={summary['C']['executed']} pnl={summary['C']['gross_sum']:.4f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
